# OpenRouter Proxy

Асинхронный FastAPI-прокси для OpenRouter с отдельным gateway-ключом, потоковой передачей ответов и готовым Docker Compose-деплоем через Nginx + Let's Encrypt.

## Локальный запуск

```bash
cp .env.example .env
# Заполните AI_GATEWAY_KEY и OPENROUTER_API_KEY. При необходимости добавьте
# вторую пару AI_GATEWAY_KEY_2 и OPENROUTER_API_KEY_2.
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Проверка:

```bash
curl http://127.0.0.1:8000/health
curl https://openrouter.ai/api/v1/models \\
  -H "X-Gateway-Key: ${AI_GATEWAY_KEY}"
```

Проксируемый путь повторяет путь OpenRouter с префиксом `/openrouter`, например:
`POST /openrouter/chat/completions`.

## Corporate LLM

Обычные вызовы остаются полностью совместимыми: запрос без корпоративных
заголовков уходит в OpenRouter по существующей паре ключей. Корпоративный
режим включается только при одновременной передаче:

- `X-Gateway-Key` (или `X-Gateway-Secret`) — ключ основного сервиса;
- `X-Organization-ID` — ID организации;
- `X-Organization-Context: Bearer <JWT>` — подписанный основной системой
  короткоживущий контекст.

JWT должен иметь `iss`, `aud=openrouter-proxy`, `exp`, `iat`, `jti`,
`organization_id` и scope `corporate_llm:proxy`. Его срок от `iat` до `exp`
не может превышать 5 минут; `organization_id` обязан совпасть с заголовком.
Прокси получает ключи верификации из `CORPORATE_JWKS_URL`; разрешены только
алгоритмы RS256 и ES256.

В корпоративном режиме разрешены только следующие `POST`-пути:

| Path | Operation |
| --- | --- |
| `/openrouter/chat/completions` | `chat_completions` |
| `/openrouter/images/generations` | `image_generation` |
| `/openrouter/audio/speech` | `audio_speech` |
| `/openrouter/audio/transcriptions` | `audio_transcription` |

SSE (`text/event-stream`) передаётся потоково, без буферизации; остальные
ответы, включая multipart transcription и бинарные audio/image-ответы,
пересылаются без изменения тела.

### Contract with the main service

Основной сервис хранит конфигурацию и шифрует API-ключ через KMS/Secret
Manager. Он предоставляет прокси защищённый mTLS endpoint:

```text
GET /internal/llm-proxy/v1/organizations/{organization_id}/routes/{operation}
Authorization: Bearer <CORPORATE_CONFIG_SERVICE_TOKEN>
```

Подключите сертификат, ключ и CA отдельным production Compose override-ом или
оркестратором: контейнер запускается непривилегированным, поэтому файлы в
`CORPORATE_CONFIG_*_FILE` должны быть смонтированы с правами чтения для UID
`65534`. Для Docker Compose есть шаблон
`docker-compose.corporate.example.yml`; заполните также три
`*_HOST_PATH` и запускайте его вместе с базовым файлом. Базовый Compose
намеренно не монтирует эти файлы и остаётся полностью совместимым с legacy
OpenRouter-режимом.

Успешный ответ строго имеет следующий вид. `base_url` — HTTPS URL корня
OpenAI-совместимого API, без query и fragment; путь операции добавляет только
прокси.

```json
{
  "organization_id": "org_123",
  "operation": "chat_completions",
  "enabled": true,
  "base_url": "https://llm.corp.example/v1",
  "auth_scheme": "bearer",
  "api_key": "plaintext-only-on-this-mtls-hop",
  "version": "42"
}
```

`auth_scheme` допускает только `bearer` и `api_key`. Хост из `base_url` должен
быть в `CORPORATE_UPSTREAM_ALLOWED_HOSTS`; произвольные URL и headers не
поддерживаются. `403`, `404` и `422` Config API означают
`operation_not_configured`, остальные ошибки — fail closed.

Redis не является источником истины: для corporate-режима обязательны
`CORPORATE_REDIS_URL` и
`CORPORATE_CACHE_ENCRYPTION_KEY` конфигурация кэшируется не более 60 секунд и
API-ключ дополнительно шифруется Fernet. В production используйте `rediss`,
ACL и Redis без persistence. При отзыве настройки основной сервис очищает
кэш:

```text
POST /internal/corporate-cache/purge
X-Internal-Cache-Key: <CORPORATE_CACHE_PURGE_KEY>
Content-Type: application/json

{"organization_id":"org_123","operation":"chat_completions"}
```

Поле `operation` опционально: при его отсутствии удаляются все четыре записи
организации. Этот endpoint должен быть доступен только из внутренней сети
основного сервиса.

Корпоративные ошибки возвращаются как `{ "error": "…", "request_id": "…" }`:
`corporate_context_invalid`, `organization_mismatch`,
`operation_not_configured`, `corporate_upstream_unavailable` или
`corporate_upstream_failed`.

## Деплой на `proxy.fastclass.ru`

1. Направьте DNS-запись `A` домена `proxy.fastclass.ru` на IP сервера.
2. Установите Docker Engine и Docker Compose Plugin.
3. Скопируйте `.env.example` в `.env` и задайте секреты.
4. Если на сервере нет собственного Nginx, запустите bundled-вариант: `docker compose --profile bundled-nginx up -d --build`.

Если Nginx уже установлен на сервере, запустите только прокси: `docker compose up -d --build proxy`. FastAPI будет доступен только локально на `127.0.0.1:${PORT}`.

Контейнер Nginx сначала поднимается с временным сертификатом, чтобы пройти HTTP-01 challenge, затем автоматически получает сертификат Let's Encrypt и переключается на HTTPS. Продление выполняется каждые 12 часов с автоматической перезагрузкой Nginx.

Порт FastAPI-прокси задается переменной `PORT` в `.env` и по умолчанию равен `8000`. Bundled Nginx автоматически использует этот порт как upstream.

Открытые порты сервера: `80/tcp` и `443/tcp`.

## Безопасность

- OpenRouter API-ключ никогда не передается клиенту и добавляется только на upstream-запросе.
- Клиентский `Authorization` игнорируется.
- Доступ к прокси разрешен только по `X-Gateway-Key` или `X-Gateway-Secret`.
- Ключи `AI_GATEWAY_KEY` и `AI_GATEWAY_KEY_2` используют соответственно
  `OPENROUTER_API_KEY` и `OPENROUTER_API_KEY_2`.
- Корпоративные ключи не передаются в JWT, браузер, обычные логи или upstream
  headers клиента. В Config API они доступны только в ответе mTLS и в Redis
  хранятся только зашифрованными.
- Docker Compose отключает access log Uvicorn, а bundled Nginx записывает URL
  без query string. Внешний Nginx должен применять тот же безопасный формат и
  не публиковать `/internal/corporate-cache/purge` вне закрытой сети.
- Секреты не должны попадать в Git: рабочий `.env` добавлен в `.gitignore`.

## Тесты

```bash
pytest -q
```
