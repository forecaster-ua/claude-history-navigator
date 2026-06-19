import json
import os
import subprocess
import logging

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

CONFIG_PATH = "/home/alexross/claude-history/llm_config.json"
VAULT_SCRIPT = "/home/alexross/secrets/get_secret.sh"
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

logger = logging.getLogger(__name__)


def get_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def set_config(updates: dict):
    cfg = get_config()
    cfg.update(updates)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _vault(section: str, account: str, field: str) -> str | None:
    try:
        result = subprocess.run(
            ["bash", VAULT_SCRIPT, section, account, field],
            capture_output=True, text=True, timeout=10
        )
        key = result.stdout.strip()
        return key if key else None
    except Exception:
        return None


def _get_key(provider: str) -> str:
    """Resolve API key for provider from vault or env."""
    sources = {
        "google":    lambda: _vault("google", "gemini", "api_key") or os.getenv("GOOGLE_API_KEY"),
        "anthropic": lambda: _vault("anthropic", "main", "api_key") or os.getenv("ANTHROPIC_API_KEY"),
        "openai":    lambda: _vault("openai", "main", "api_key") or os.getenv("OPENAI_API_KEY"),
        "deepseek":  lambda: _vault("deepseek", "main", "api_key") or os.getenv("DEEPSEEK_API_KEY"),
    }
    resolver = sources.get(provider)
    if not resolver:
        raise ValueError(f"Unknown provider: {provider}")
    key = resolver()
    if not key:
        raise ValueError(f"No API key found for provider '{provider}'")
    return key


PROVIDERS = {
    "google":    {"env_key": "GOOGLE_API_KEY",    "label": "Google Gemini"},
    "anthropic": {"env_key": "ANTHROPIC_API_KEY", "label": "Anthropic Claude"},
    "openai":    {"env_key": "OPENAI_API_KEY",    "label": "OpenAI"},
    "deepseek":  {"env_key": "DEEPSEEK_API_KEY",  "label": "DeepSeek"},
}

DEFAULT_MODELS = {
    "google":    "gemini-2.5-flash",
    "anthropic": "claude-sonnet-4-6",
    "openai":    "gpt-5.4-mini",
    "deepseek":  "deepseek-v4-flash",
}

DEFAULT_TEMPERATURES = {
    "google":    0.4,
    "anthropic": 0.3,
    "openai":    0.3,
    "deepseek":  0.5,
}


def get_available_providers() -> list[dict]:
    """Return all providers with whether they have a key configured."""
    result = []
    for pid, info in PROVIDERS.items():
        has_key = False
        try:
            k = _get_key(pid)
            has_key = bool(k)
        except Exception:
            pass
        result.append({
            "id": pid,
            "label": info["label"],
            "has_key": has_key,
            "default_model": DEFAULT_MODELS.get(pid, ""),
            "default_temperature": DEFAULT_TEMPERATURES.get(pid, 0.4),
        })
    return result


def set_env_key(provider: str, api_key: str):
    """Write API key to .env file."""
    info = PROVIDERS.get(provider)
    if not info:
        raise ValueError(f"Unknown provider: {provider}")
    env_key = info["env_key"]
    lines = []
    found = False
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                if line.startswith(f"{env_key}="):
                    lines.append(f"{env_key}={api_key}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{env_key}={api_key}\n")
    with open(ENV_PATH, "w") as f:
        f.writelines(lines)
    os.environ[env_key] = api_key


async def generate(prompt: str, system: str = "",
                   provider: str = None, model: str = None,
                   temperature: float = None) -> str:
    cfg = get_config()
    provider = provider or cfg["provider"]
    model = model or cfg["model"]
    key = _get_key(provider)

    if temperature is None:
        temperature = cfg.get("temperature", DEFAULT_TEMPERATURES.get(provider, 0.4))

    logger.info(f"LLM generate: provider={provider} model={model} temperature={temperature}")

    if provider == "google":
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        config = types.GenerateContentConfig(
            system_instruction=system if system else None,
            max_output_tokens=16384,
            temperature=temperature,
        )
        response = client.models.generate_content(
            model=model, contents=prompt, config=config
        )
        return response.text

    elif provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=model,
            max_tokens=16384,
            temperature=temperature,
            system=system or "You are a helpful assistant.",
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text

    elif provider in ("openai", "deepseek"):
        import openai as oai
        base_url = "https://api.deepseek.com" if provider == "deepseek" else None
        client = oai.OpenAI(api_key=key, base_url=base_url)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=model, messages=messages, max_tokens=16384, temperature=temperature
        )
        return resp.choices[0].message.content

    raise ValueError(f"Unsupported provider: {provider}")
