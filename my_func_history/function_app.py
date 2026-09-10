import os
import base64
import json
import logging
from datetime import datetime, timezone
import azure.functions as func
from pymongo import MongoClient

app = func.FunctionApp()

# Inicialização global da conexão com o MongoDB (Reutiliza a conexão entre chamadas para melhor performance)
mongo_conn_string = os.getenv("MONGO_DB_CONNECTION_STRING")
mongo_db_name = os.getenv("MONGO_DB_NAME")
queue_name = os.getenv("QUEUE_NAME", "aprovia-chat-history")

mongo_client = None
if mongo_conn_string:
    mongo_client = MongoClient(mongo_conn_string)

@app.queue_trigger(
    arg_name="msg", 
    queue_name=queue_name,
    connection="AzureWebJobsStorage"
)
def ConsumirHistoricoQueue(msg: func.QueueMessage) -> None:
    try:
        # 1. Lê a mensagem vinda do Azure Queue Storage
        if mongo_client is None or not mongo_db_name:
            raise RuntimeError(
                "MONGO_DB_CONNECTION_STRING e MONGO_DB_NAME são obrigatórios."
            )

        body = msg.get_body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = json.loads(base64.b64decode(body).decode("utf-8"))
        
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
        db = mongo_client[mongo_db_name]
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