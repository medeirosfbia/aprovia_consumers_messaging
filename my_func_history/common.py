import os
import base64
import json
from pymongo import MongoClient

# Conexão compartilhada entre as funções (reutilizada entre chamadas)
mongo_conn_string = os.getenv("MONGO_DB_CONNECTION_STRING")
mongo_db_name = os.getenv("MONGO_DB_NAME")

mongo_client = None
if mongo_conn_string:
    mongo_client = MongoClient(mongo_conn_string)


def get_db():
    if mongo_client is None or not mongo_db_name:
        raise RuntimeError(
            "MONGO_DB_CONNECTION_STRING e MONGO_DB_NAME são obrigatórios."
        )
    return mongo_client[mongo_db_name]


def decode_queue_body(body: bytes) -> dict:
    """Aceita JSON UTF-8 ou JSON codificado em Base64 (formato do produtor)."""
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return json.loads(base64.b64decode(body).decode("utf-8"))
