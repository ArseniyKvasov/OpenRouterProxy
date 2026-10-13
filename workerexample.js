/**
 * Cloudflare Worker implementation of the OpenRouter Proxy with Telegram support
 * Based on commit 0b0a699 (Telegram API support) from the original FastAPI proxy
 *
 * Deploy with: wrangler deploy
 * Configure secrets:
 *   wrangler secret put AI_GATEWAY_KEY
 *   wrangler secret put OPENROUTER_API_KEY
 *   wrangler secret put TELEGRAM_BOT_TOKEN
 *   wrangler secret put TELEGRAM_USER_ID
 */

const HOP_BY_HOP_HEADERS = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
  'host',
  'content-length',
]);

const SENSITIVE_REQUEST_HEADERS = new Set([
  'authorization',
  'x-gateway-key',
  'x-gateway-secret',
]);

/**
 * Validates the gateway key from request headers
 */
function getGatewayKey(request) {
  return request.headers.get('x-gateway-key') || request.headers.get('x-gateway-secret') || '';
}

/**
 * Constant-time comparison to prevent timing attacks
 */
function isAuthorized(receivedKey, expectedKey) {
  if (!receivedKey || !expectedKey) return false;
  if (receivedKey.length !== expectedKey.length) return false;

  let result = 0;
  for (let i = 0; i < receivedKey.length; i++) {
    result |= receivedKey.charCodeAt(i) ^ expectedKey.charCodeAt(i);
  }
  return result === 0;
}

/**
 * Builds headers for the upstream OpenRouter request
 */
function buildUpstreamHeaders(request, openrouterApiKey) {
  const headers = {};

  for (const [name, value] of request.headers.entries()) {
    const normalizedName = name.toLowerCase();
    if (HOP_BY_HOP_HEADERS.has(normalizedName) || SENSITIVE_REQUEST_HEADERS.has(normalizedName)) {
      continue;
    }
    headers[name] = value;
  }

  headers['Authorization'] = `Bearer ${openrouterApiKey}`;
  return headers;
}

/**
 * Filters response headers from upstream
 */
function buildResponseHeaders(upstreamHeaders) {
  const headers = {};

  for (const [name, value] of upstreamHeaders.entries()) {
    if (!HOP_BY_HOP_HEADERS.has(name.toLowerCase())) {
      headers[name] = value;
    }
  }

  return headers;
}

/**
 * Handles OpenRouter proxy requests
 */
async function handleOpenRouterProxy(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.slice('/openrouter/'.length);

  const gatewayKey = getGatewayKey(request);
  if (!isAuthorized(gatewayKey, env.AI_GATEWAY_KEY)) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const baseUrl = env.OPENROUTER_BASE_URL || 'https://routerai.ru/api/v1';
  const upstreamUrl = `${baseUrl.replace(/\/$/, '')}/${path}${url.search}`;

  let body = null;
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    body = await request.arrayBuffer();
  }

  const upstreamHeaders = buildUpstreamHeaders(request, env.OPENROUTER_API_KEY);

  try {
    const upstreamResponse = await fetch(upstreamUrl, {
      method: request.method,
      headers: upstreamHeaders,
      body: body,
      redirect: 'manual',
    });

    const responseHeaders = buildResponseHeaders(upstreamResponse.headers);

    // Read and transform response body for cost adjustment
    const contentType = upstreamResponse.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      const responseText = await upstreamResponse.text();
      let responseData;
      try {
        responseData = JSON.parse(responseText);
      } catch {
        // Not valid JSON, return as-is
        return new Response(responseText, {
          status: upstreamResponse.status,
          statusText: upstreamResponse.statusText,
          headers: responseHeaders,
        });
      }

      // Transform cost field: divide by 80, round to 8 decimal places
      function transformCost(obj) {
        if (!obj || typeof obj !== 'object') return obj;

        // Handle common cost fields in OpenRouter-compatible responses
        const costFields = ['cost', 'total_cost', 'usage.cost', 'usage.total_cost'];
        
        for (const field of costFields) {
          const keys = field.split('.');
          let current = obj;
          for (let i = 0; i < keys.length - 1; i++) {
            if (!current[keys[i]] || typeof current[keys[i]] !== 'object') {
              current = null;
              break;
            }
            current = current[keys[i]];
          }
          if (current && typeof current[keys[keys.length - 1]] === 'number') {
            const originalCost = current[keys[keys.length - 1]];
            current[keys[keys.length - 1]] = Math.round((originalCost / 80) * 1e8) / 1e8;
          }
        }

        // Recursively process nested objects and arrays
        for (const key of Object.keys(obj)) {
          if (obj[key] && typeof obj[key] === 'object') {
            transformCost(obj[key]);
          }
        }

        return obj;
      }

      transformCost(responseData);

      return new Response(JSON.stringify(responseData), {
        status: upstreamResponse.status,
        statusText: upstreamResponse.statusText,
        headers: responseHeaders,
      });
    }

    // Non-JSON response, return as-is
    return new Response(upstreamResponse.body, {
      status: upstreamResponse.status,
      statusText: upstreamResponse.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    console.error('OpenRouter upstream request failed:', error);
    return new Response(JSON.stringify({ error: 'Upstream OpenRouter request failed' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

/**
 * Handles Telegram sendMessage requests
 * Accepts JSON payload, injects chat_id from environment, forwards to Telegram API
 */
async function handleTelegramSendMessage(request, env) {
  const gatewayKey = getGatewayKey(request);
  if (!isAuthorized(gatewayKey, env.AI_GATEWAY_KEY)) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const telegramToken = (env.TELEGRAM_BOT_TOKEN || '').trim();
  const telegramUserId = (env.TELEGRAM_USER_ID || '').trim();

  if (!telegramToken || !telegramUserId) {
    return new Response(JSON.stringify({ error: 'Telegram proxy is not configured' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: 'Request body must be valid JSON' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  if (!payload || typeof payload !== 'object') {
    return new Response(JSON.stringify({ error: 'Request body must be a JSON object' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const text = payload.text;
  if (typeof text !== 'string' || !text.trim()) {
    return new Response(JSON.stringify({ error: 'Telegram message text is required' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // Chat ID is always controlled by the proxy environment (security)
  payload.chat_id = telegramUserId;

  const telegramUrl = `https://api.telegram.org/bot${telegramToken}/sendMessage`;

  try {
    const telegramResponse = await fetch(telegramUrl, {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    const responseHeaders = buildResponseHeaders(telegramResponse.headers);

    return new Response(telegramResponse.body, {
      status: telegramResponse.status,
      statusText: telegramResponse.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    console.error('Telegram upstream request failed:', error);
    return new Response(JSON.stringify({ error: 'Telegram upstream request failed' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

/**
 * Main request handler
 */
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Health check endpoint
    if (url.pathname === '/health') {
      return new Response(JSON.stringify({ status: 'ok' }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // OpenRouter proxy endpoint
    if (url.pathname.startsWith('/openrouter/')) {
      return handleOpenRouterProxy(request, env);
    }

    // Telegram sendMessage endpoint
    if (url.pathname === '/telegram/sendMessage' && request.method === 'POST') {
      return handleTelegramSendMessage(request, env);
    }

    // 404 for unknown routes
    return new Response(JSON.stringify({ error: 'Not Found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  },
};