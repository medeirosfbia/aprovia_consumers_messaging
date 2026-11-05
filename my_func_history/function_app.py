import os
import logging
from datetime import datetime, timezone
import azure.functions as func

from common import decode_queue_body, get_db
from notifications import bp as notifications_bp

app = func.FunctionApp()
app.register_functions(notifications_bp)

queue_name = os.getenv("QUEUE_NAME", "aprovia-chat-history")

@app.queue_trigger(
    arg_name="msg", 
    queue_name=queue_name,
    connection="AzureWebJobsStorage"
)
def ConsumirHistoricoQueue(msg: func.QueueMessage) -> None:
    try:
        # 1. Lê a mensagem vinda do Azure Queue Storage
        db = get_db()
        payload = decode_queue_body(msg.get_body())

        logging.info(
            "Processando mensagem de histórico para o usuário: %s",
            payload.get("userId"),
        )

        # Extrai os parâmetros recebidos
        user_id = payload.get("userId")
        chat_id = payload.get("chatId")
        role = payload.get("role")
        content = payload.get("content")

        if not user_id or not chat_id or not content:
            raise ValueError("Payload incompleto recebido da fila.")

        # 2. Conecta à coleção 'conversas' do MongoDB
        collection = db["assistant"]

        # 3. Executa a atualização atômica exata no MongoDB
        agora = datetime.now(timezone.utc)
        
        result = collection.update_one(
            {
                "user_id": user_id, 
                "chat_id": chat_id
            },
            {
                "$push": {
                    "messages": {
                        "role": role,
                        "content": content,
                        "timestamp": agora
                    }
                },
                "$set": {
                    "updated_at": agora
                }
            }
        )

        if result.matched_count > 0:
            logging.info(f"Histórico atualizado com sucesso para chat_id: {chat_id}")
        else:
            raise ValueError(
                f"Nenhum documento encontrado para user_id={user_id} "
                f"e chat_id={chat_id}."
            )

    except Exception as e:
        logging.error("Erro ao processar mensagem da fila no MongoDB: %s", e)
        raise