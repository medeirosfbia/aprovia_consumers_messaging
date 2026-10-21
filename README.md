# Consumer de histórico de chats

Azure Function responsável por consumir mensagens da fila do Azure Storage
Queue e atualizar o histórico de conversas no MongoDB.

## O que este serviço faz

A função `ConsumirHistoricoQueue`:

1. recebe uma mensagem da fila `aprovia-chat-history`;
2. interpreta o corpo como JSON UTF-8 ou, como fallback, como JSON codificado
   em Base64;
3. valida os campos do evento;
4. localiza o chat pelo `user_id` e `chat_id`;
5. adiciona a mensagem na lista `messages`;
6. atualiza o campo `updated_at`.

O documento é gravado na coleção `assistant` do banco definido em
`MONGO_DB_NAME`.

## Estrutura

```text
consumers_messaging/
├── README.md
└── my_func_history/
    ├── function_app.py
    ├── host.json
    ├── requirements.txt
    ├── example-local.setting.json
    └── local.settings.json       # local, não versionar
```

## Pré-requisitos

Instale:

- Python 3.10 ou superior;
- Azure Functions Core Tools v4;
- Azure Storage Emulator/Azurite para execução totalmente local;
- MongoDB local ou MongoDB Atlas;
- Azure CLI, caso o serviço seja publicado na Azure.

Verifique a instalação:

```bash
python3 --version
func --version
azurite --version
```

## Configuração local

Entre no diretório da Function:

```bash
cd consumers_messaging/my_func_history
```

Crie um ambiente virtual e instale as dependências:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

No Windows, ative o ambiente com:

```powershell
.venv\Scripts\Activate.ps1
```

Crie `local.settings.json` nesse mesmo diretório. O arquivo já está no
`.gitignore` e não deve ser commitado:

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "MONGO_DB_CONNECTION_STRING": "mongodb://localhost:27017/",
    "MONGO_DB_NAME": "aprovia_db",
    "QUEUE_NAME": "aprovia-chat-history"
  }
}
```

Para usar MongoDB Atlas, substitua `MONGO_DB_CONNECTION_STRING` pela URI do
cluster. Não compartilhe nem versione uma URI que contenha senha.

O arquivo `example-local.setting.json` é um modelo. Os valores com
`<YOUR_ACCOUNT_NAME>` e `<YOUR_ACCOUNT_KEY>` são placeholders e precisam ser
substituídos apenas em uma configuração local privada.

## Executar localmente com Azurite

Em um terminal separado, inicie o Azurite:

```bash
azurite
```

Com o ambiente virtual ativado, inicie a Function a partir de
`my_func_history`:

```bash
func start
```

Uma inicialização bem-sucedida deve listar a função de fila
`ConsumirHistoricoQueue` e indicar que o host local está em execução.

Com `UseDevelopmentStorage=true`, a Function usa o armazenamento local do
Azurite. A fila precisa existir antes do teste. Caso necessário, crie-a com
Azure Storage Explorer, Azure CLI ou uma ferramenta compatível com o endpoint
local do Azurite.

## Evento esperado

Cada mensagem deve conter os campos abaixo:

| Campo | Obrigatório | Descrição |
|---|---:|---|
| `userId` | Sim | Identificador do usuário |
| `chatId` | Sim | Identificador do chat |
| `role` | Não | Papel da mensagem, normalmente `user` ou `assistant` |
| `content` | Sim | Conteúdo textual da mensagem |

Exemplo:

```json
{
  "userId": "1",
  "chatId": "chat-123",
  "role": "assistant",
  "content": "A resposta do tutor."
}
```

O consumidor adiciona a mensagem no documento que corresponde a:

```json
{
  "user_id": "1",
  "chat_id": "chat-123"
}
```

O resultado esperado é semelhante a:

```json
{
  "user_id": "1",
  "chat_id": "chat-123",
  "messages": [
    {
      "role": "assistant",
      "content": "A resposta do tutor.",
      "timestamp": "<data UTC>"
    }
  ],
  "updated_at": "<data UTC>"
}
```

## Teste manual

Para testar o consumidor:

1. inicie o Azurite;
2. inicie a Function com `func start`;
3. crie ou escolha um documento existente na coleção `assistant`;
4. publique na fila uma mensagem com o formato do exemplo;
5. verifique os logs da Function;
6. confirme no MongoDB que `messages` e `updated_at` foram atualizados.

Se não existir um documento com o par `user_id`/`chat_id`, a função gera erro
para que a mensagem possa ser tratada novamente pela infraestrutura da fila.

## Variáveis de ambiente

| Variável | Obrigatória | Uso |
|---|---:|---|
| `AzureWebJobsStorage` | Sim | Conexão usada pelo trigger da fila |
| `FUNCTIONS_WORKER_RUNTIME` | Sim | Deve ser `python` |
| `MONGO_DB_CONNECTION_STRING` | Sim | URI de conexão com MongoDB |
| `MONGO_DB_NAME` | Sim | Banco que contém a coleção `assistant` |
| `QUEUE_NAME` | Não | Nome da fila; padrão `aprovia-chat-history` |

Para desenvolvimento local, use:

```text
AzureWebJobsStorage=UseDevelopmentStorage=true
FUNCTIONS_WORKER_RUNTIME=python
QUEUE_NAME=aprovia-chat-history
```

Para Azure, `AzureWebJobsStorage` deve apontar para uma conta real de Storage
com permissão para consumir a fila.

## Logs e erros

Mensagens processadas com sucesso geram logs informando o `chat_id`.

A função falha explicitamente quando:

- MongoDB não foi configurado;
- `MONGO_DB_NAME` não foi informado;
- o corpo não é um JSON válido nem um JSON Base64 válido;
- faltam `userId`, `chatId` ou `content`;
- não existe um documento correspondente no MongoDB;
- ocorre falha na conexão ou na atualização do MongoDB.

Não remova o `raise` do tratamento de erro: a exceção permite que o runtime
identifique a mensagem como falha e aplique a política de retry configurada.

## Testes e validações

Este diretório atualmente não possui uma suíte automatizada. Antes de abrir um
PR, valide pelo menos:

1. inicialização da Function sem erros;
2. consumo de um JSON UTF-8;
3. consumo de um JSON Base64;
4. rejeição de payload sem `userId`, `chatId` ou `content`;
5. atualização atômica do documento no MongoDB;
6. comportamento quando o chat não existe.

Para conferir a sintaxe do arquivo:

```bash
python -m py_compile my_func_history/function_app.py
```

## Publicação na Azure

A publicação deve ser feita a partir do diretório da Function, usando o Azure
Functions Core Tools ou o processo padrão adotado pelo time:

```bash
cd consumers_messaging/my_func_history
func azure functionapp publish <NOME_DA_FUNCTION_APP>
```

Configure as seguintes Application Settings na Function App:

- `AzureWebJobsStorage`;
- `FUNCTIONS_WORKER_RUNTIME=python`;
- `MONGO_DB_CONNECTION_STRING`;
- `MONGO_DB_NAME`;
- `QUEUE_NAME=aprovia-chat-history`.

Não publique `local.settings.json`. No ambiente Azure, as configurações devem
ser cadastradas nas Application Settings da Function App.

## Segurança

- Nunca versione `local.settings.json`.
- Nunca coloque senha de MongoDB ou `AccountKey` em commits, issues ou logs.
- Use uma conta de Storage e um banco separados para desenvolvimento quando
  possível.
- Restrinja a rede e as permissões do MongoDB e do Storage.
- Não registre o corpo completo de mensagens se ele puder conter dados
  sensíveis.
- Em caso de vazamento de credenciais, revogue e gere novas credenciais
  imediatamente.
