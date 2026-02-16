import os
import time
import requests
from dotenv import load_dotenv
load_dotenv(override=True)

API_KEY = os.getenv("PROXYAPI_KEY")

if not API_KEY: 
    raise SystemExit("Нет PROXYAPI_KEY. Добавь ключ в .env (PROXYAPI_KEY=...).")

HEADERS = {"Authorization": f"Bearer {API_KEY}"}
    
def check_models() -> dict:
    """
    Проверка доступности OpenAI-совместимого API ProxyAPI.
    По документации базовый адрес: https://openai.api.proxyapi.ru/v1
    Эндпоинт: GET /v1/models (на практике: GET https://openai.api.proxyapi.ru/v1/models)
    """
    url = "https://openai.api.proxyapi.ru/v1/models"
    t0 = time.time()
    r = requests.get(url, headers=HEADERS, timeout=20)
    ms = int((time.time() - t0) * 1000)

    ok = (200 <= r.status_code < 300)
    payload = None
    try:
        payload = r.json()
    except Exception:
        payload = {"raw": r.text[:500]}

    models_count = None
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        models_count = len(payload["data"])

    return {
        "check": "openai_compatible_models",
        "url": url,
        "ok": ok,
        "http_status": r.status_code,
        "latency_ms": ms,
        "models_count": models_count,
        "sample": (payload.get("data", [])[:1] if isinstance(payload, dict) else None),
    }

def check_balance() -> dict:
    """
    Проверка баланса ProxyAPI.
    ВАЖНО: для ключа надо включить разрешение "Запрос баланса" в личном кабинете.
    Эндпоинт: GET https://api.proxyapi.ru/proxyapi/balance
    """
    url = "https://api.proxyapi.ru/proxyapi/balance"
    t0 = time.time()
    r = requests.get(url, headers=HEADERS, timeout=20)
    ms = int((time.time() - t0) * 1000)

    ok = (200 <= r.status_code < 300)
    payload = None
    try:
        payload = r.json()
    except Exception:
        payload = {"raw": r.text[:500]}

    return {
        "check": "proxyapi_balance",
        "url": url,
        "ok": ok,
        "http_status": r.status_code,
        "latency_ms": ms,
        "response": payload,
    }

def main():
    
    results = []
    results.append(check_models())
    results.append(check_balance())

    print("=== ProxyAPI status ===")
    for item in results:
        status = "OK" if item["ok"] else "FAIL"
        print(f"\n[{status}] {item['check']}")
        print(f"URL: {item['url']}")
        print(f"HTTP: {item['http_status']}, latency: {item['latency_ms']} ms")
        if item["check"] == "openai_compatible_models":
            print(f"Models count: {item.get('models_count')}")
            if item.get("sample") is not None:
                print(f"Sample: {item['sample']}")
        else:
            print(f"Response: {item.get('response')}")

if __name__ == "__main__":
    main()
