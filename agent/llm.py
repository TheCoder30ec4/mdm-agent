"""Chat model for the provider in effect (contracts/settings.md, Providers). Imports are lazy: only the chosen
provider's package is loaded, and building a model never calls the API."""
from agent.settings import BY_ID, ENV_KEY


class NotConfigured(RuntimeError):
    pass


def make(cfg: dict, timeout=None, max_retries=2):
    """cfg is settings.effective(). Raises NotConfigured with a message for the user (never the key)."""
    provider, model = cfg["provider"], cfg["model"]
    key = cfg["keys"].get(provider)
    label = BY_ID[provider]["label"]
    if provider in ENV_KEY and not key:
        raise NotConfigured(f"{ENV_KEY[provider]} is not set and no {label} key is saved. "
                            "Choose an AI provider in Settings.")
    if provider == "compatible" and not (cfg["base_url"] and model):
        raise NotConfigured("The OpenAI-compatible provider needs a base URL and a model. Set them in Settings.")
    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=model, api_key=key, temperature=0.2, max_retries=max_retries, timeout=timeout)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=key, max_retries=max_retries, timeout=timeout)
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, api_key=key, retries=max_retries, request_timeout=timeout)
    from langchain_openai import ChatOpenAI  # openai and compatible
    return ChatOpenAI(model=model, api_key=key or "none", base_url=cfg["base_url"] if provider == "compatible" else None,
                      max_retries=max_retries, timeout=timeout)  # local servers (Ollama) ignore the key
