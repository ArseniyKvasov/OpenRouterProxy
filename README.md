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
- Секреты не должны попадать в Git: рабочий `.env` добавлен в `.gitignore`.

## Тесты

```bash
pytest -q
```
