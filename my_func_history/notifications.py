import os
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import azure.functions as func
import requests
from pymongo import ASCENDING

from common import decode_queue_body, get_db

bp = func.Blueprint()

notifications_queue_name = os.getenv("NOTIFICATIONS_QUEUE_NAME", "aprovia-notifications")
push_tokens_collection_name = os.getenv("PUSH_TOKENS_COLLECTION", "push_tokens")
push_tickets_collection_name = os.getenv("PUSH_TICKETS_COLLECTION", "push_tickets")
user_activity_collection_name = os.getenv("USER_ACTIVITY_COLLECTION", "user_activity")
# "Hoje" é contado neste fuso; precisa ser igual ao REMINDER_TIMEZONE da API
reminder_timezone = ZoneInfo(os.getenv("REMINDER_TIMEZONE", "America/Sao_Paulo"))
# CRON em UTC (o timer do Functions no Linux não aceita fuso): 18h UTC = 15h em Brasília
daily_reminder_schedule = os.getenv("DAILY_REMINDER_SCHEDULE", "0 0 18 * * *")
# Opcional: só é exigido se a "enhanced push security" estiver ativa no Expo
expo_access_token = os.getenv("EXPO_ACCESS_TOKEN")
# Opcional: o canal precisa existir no app; sem ele o Expo usa o canal padrão
android_channel_id = os.getenv("EXPO_ANDROID_CHANNEL_ID")

EXPO_SEND_URL = "https://exp.host/--/api/v2/push/send"
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
TODOS_USUARIOS = "all"
MAX_MENSAGENS_POR_REQUISICAO = 100
MAX_RECIBOS_POR_REQUISICAO = 1000
TENTATIVAS_HTTP = 3
# O Expo recomenda consultar os recibos ~15 min após o envio; eles expiram em 24 h
ESPERA_RECIBO = timedelta(minutes=15)
VALIDADE_TICKET_SEGUNDOS = 24 * 60 * 60


class ExpoTemporaryError(Exception):
    pass


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _expo_post(url, payload):
    """POST ao Expo com backoff exponencial em erros de rede, 429 e 5xx."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
    }
    if expo_access_token:
        headers["Authorization"] = f"Bearer {expo_access_token}"

    for tentativa in range(TENTATIVAS_HTTP):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=15)
        except requests.RequestException as error:
            erro = ExpoTemporaryError(f"Falha de rede ao chamar o Expo: {error}")
        else:
            if response.status_code != 429 and response.status_code < 500:
                # 4xx (exceto 429) é payload/credencial inválida: repetir não resolve
                response.raise_for_status()
                return response.json()
            erro = ExpoTemporaryError(f"Expo respondeu HTTP {response.status_code}")

        if tentativa < TENTATIVAS_HTTP - 1:
            time.sleep(2 ** tentativa)
    raise erro


def _validate_event(payload):
    title = payload.get("title")
    body = payload.get("body")
    recipients = payload.get("recipients", TODOS_USUARIOS)
    data = payload.get("data") or {}

    if not title or not body:
        raise ValueError("Evento de notificação sem title ou body.")
    if recipients != TODOS_USUARIOS and not (
        isinstance(recipients, list) and recipients
    ):
        raise ValueError('recipients deve ser "all" ou uma lista de usuários.')
    if not isinstance(data, dict):
        raise ValueError("data deve ser um objeto.")

    return title, body, recipients, data


def _find_tokens(db, recipients):
    filtro = {} if recipients == TODOS_USUARIOS else {"user_id": {"$in": recipients}}
    return [
        doc["token"]
        for doc in db[push_tokens_collection_name].find(filtro, {"_id": 0, "token": 1})
    ]


def _build_message(token, title, body, data):
    message = {
        "to": token,
        "title": title,
        "body": body,
        "data": data,
        "sound": "default",
    }
    if android_channel_id:
        message["channelId"] = android_channel_id
    return message


def _remove_tokens(db, tokens):
    if tokens:
        result = db[push_tokens_collection_name].delete_many({"token": {"$in": list(tokens)}})
        logging.info("%s token(s) inválido(s) removido(s).", result.deleted_count)


def ensure_ticket_indexes(db):
    db[push_tickets_collection_name].create_index(
        [("created_at", ASCENDING)],
        expireAfterSeconds=VALIDADE_TICKET_SEGUNDOS,
        name="push_ticket_ttl",
    )


def send_notification(db, payload):
    """Envia o evento a todos os aparelhos dos destinatários. Retorna o total enviado."""
    title, body, recipients, data = _validate_event(payload)
    # O tipo vai junto para o app decidir a navegação ao tocar na notificação
    data = {**data, "type": payload.get("type")}

    tokens = _find_tokens(db, recipients)
    if not tokens:
        logging.info("Nenhum aparelho cadastrado para os destinatários.")
        return 0

    agora = datetime.now(timezone.utc)
    invalidos = set()
    tickets_ok = []

    for lote in _chunks(tokens, MAX_MENSAGENS_POR_REQUISICAO):
        resposta = _expo_post(
            EXPO_SEND_URL, [_build_message(token, title, body, data) for token in lote]
        )
        if resposta.get("errors"):
            raise RuntimeError(f"Expo rejeitou o lote: {resposta['errors']}")

        # Os tickets vêm na mesma ordem das mensagens
        for token, ticket in zip(lote, resposta.get("data", [])):
            if ticket.get("status") == "ok":
                tickets_ok.append({"ticket_id": ticket["id"], "token": token, "created_at": agora})
            elif (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                invalidos.add(token)
            else:
                logging.warning("Falha ao enviar push: %s", ticket.get("message"))

    _remove_tokens(db, invalidos)
    if tickets_ok:
        db[push_tickets_collection_name].insert_many(tickets_ok)
    return len(tickets_ok)


def check_receipts(db, now=None):
    """Consulta os recibos pendentes e remove os aparelhos desinstalados."""
    now = now or datetime.now(timezone.utc)
    tickets = list(db[push_tickets_collection_name].find(
        {"created_at": {"$lte": now - ESPERA_RECIBO}},
        {"_id": 0, "ticket_id": 1, "token": 1},
    ).limit(10 * MAX_RECIBOS_POR_REQUISICAO))

    invalidos = set()
    for lote in _chunks(tickets, MAX_RECIBOS_POR_REQUISICAO):
        token_por_ticket = {ticket["ticket_id"]: ticket["token"] for ticket in lote}
        recibos = _expo_post(EXPO_RECEIPTS_URL, {"ids": list(token_por_ticket)}).get("data", {})

        for ticket_id, recibo in recibos.items():
            if recibo.get("status") != "error":
                continue
            erro = (recibo.get("details") or {}).get("error")
            if erro == "DeviceNotRegistered":
                invalidos.add(token_por_ticket[ticket_id])
            else:
                # MessageTooBig, InvalidCredentials, MismatchSenderId... exigem correção manual
                logging.error("Recibo de push com erro %s: %s", erro, recibo.get("message"))

        db[push_tickets_collection_name].delete_many(
            {"ticket_id": {"$in": list(token_por_ticket)}}
        )

    _remove_tokens(db, invalidos)
    return len(tickets)


def send_daily_reminder(db, now=None):
    """Lembra quem tem aparelho cadastrado e ainda não usou o app hoje."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(reminder_timezone).date().isoformat()

    com_aparelho = set(db[push_tokens_collection_name].distinct("user_id"))
    ativos_hoje = set(db[user_activity_collection_name].distinct(
        "user_id", {"last_active_date": today}
    ))
    pendentes = sorted(com_aparelho - ativos_hoje)
    if not pendentes:
        logging.info("Todos os usuários com aparelho já usaram o app hoje.")
        return 0

    return send_notification(db, {
        "type": "daily_reminder",
        "recipients": pendentes,
        "title": "Bora estudar hoje?",
        "body": "Você ainda não estudou hoje. Que tal alguns minutos no AprovIA?",
    })


@bp.queue_trigger(
    arg_name="msg",
    queue_name=notifications_queue_name,
    connection="AzureWebJobsStorage"
)
def EnviarNotificacaoQueue(msg: func.QueueMessage) -> None:
    try:
        payload = decode_queue_body(msg.get_body())
        logging.info("Processando notificação do tipo: %s", payload.get("type"))

        enviados = send_notification(get_db(), payload)
        logging.info("Notificação enviada para %s aparelho(s).", enviados)

    except Exception as e:
        # Relança para o runtime aplicar o retry (e a poison queue depois de 5 falhas)
        logging.error("Erro ao processar notificação da fila: %s", e)
        raise


@bp.timer_trigger(arg_name="timer", schedule="0 */15 * * * *")
def VerificarRecibosPush(timer: func.TimerRequest) -> None:
    try:
        db = get_db()
        ensure_ticket_indexes(db)
        verificados = check_receipts(db)
        logging.info("%s recibo(s) de push verificado(s).", verificados)
    except Exception as e:
        logging.error("Erro ao verificar recibos de push: %s", e)
        raise


@bp.timer_trigger(arg_name="timer", schedule=daily_reminder_schedule)
def EnviarLembreteDiario(timer: func.TimerRequest) -> None:
    try:
        enviados = send_daily_reminder(get_db())
        logging.info("Lembrete diário enviado para %s aparelho(s).", enviados)
    except Exception as e:
        logging.error("Erro ao enviar lembrete diário: %s", e)
        raise
