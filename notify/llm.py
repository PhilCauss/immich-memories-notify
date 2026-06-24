"""LLM image validation using OpenAI-compatible API."""

import base64
import logging
from pathlib import Path

import requests

PROMPT_PATH = Path(__file__).parent / "prompts" / "image_validation_prompt.txt"
TITLE_PROMPT_PATH = Path(__file__).parent / "prompts" / "title_generation_prompt_persons.txt"


def load_prompt(prompt_path=None):
    """Load the validation prompt from the default text file."""
    path = Path(prompt_path) if prompt_path else PROMPT_PATH
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return (
            "You are a photo quality validator. Reply only 'Yes' or 'No' "
            "based on whether this is a good quality photo worth keeping."
        )


def validate_image(image_data: bytes, config: dict = None) -> bool | None:
    """
    Validate an image using an OpenAI-compatible chat completion API.

    Reads LLM config from the provided config dict (or from config.yaml
    if not supplied). The caller only needs to provide the image data.

    Args:
        image_data: Raw image bytes.
        config: Application config dict (must contain 'llm' section with
                'url', 'model', 'api_key'). If omitted, loads config.yaml.

    Returns:
        True if image should be kept, False if rejected,
        None if validation could not be performed (proceeds anyway).
    """
    logger = logging.getLogger("immich-memories-notify")

    # Load config if not provided
    if config is None:
        from notify.config import load_config

        config = load_config()

    llm_config = config.get("llm", {})
    llm_url = llm_config.get("url", "")
    model = llm_config.get("model", "")
    api_key = llm_config.get("api_key", "")

    if not llm_url or not model:
        logger.debug("LLM validation skipped: no url/model configured")
        return None

    prompt = load_prompt()

    base64_image = base64.b64encode(image_data).decode("utf-8")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                    },
                ],
            }
        ],
        "temperature": 0.0,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        api_url = f"{llm_url.rstrip('/')}/chat/completions"
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        result = response.json()
        answer = result["choices"][0]["message"]["content"].strip().lower()

        if answer.startswith("yes"):
            logger.info("  LLM validation: KEEP")
            return True
        elif answer.startswith("no"):
            logger.info("  LLM validation: REJECT")
            return False
        else:
            logger.warning(
                f"LLM returned unexpected answer '{answer}', proceeding anyway"
            )
            return None

    except Exception as e:
        logger.warning(f"LLM validation failed, proceeding anyway: {e}")
        return None


EVENT_PROMPT_MAP = {
    "person": TITLE_PROMPT_PATH,
    "then_and_now": TITLE_PROMPT_PATH.parent / "title_generation_prompt_then_and_now.txt",
    "trip_highlights": TITLE_PROMPT_PATH.parent / "title_generation_prompt_trip_highlights.txt",
    "weekly_collage": TITLE_PROMPT_PATH.parent / "title_generation_prompt_weekly_collage.txt",
}

DEFAULT_TITLE_PROMPT = (
    "Generate a short, fun title (max 12 words) for a photo notification "
    "featuring {person_name}. Reply with only the title."
)


def generate_title(
    image_data: bytes,
    context: dict,
    event_type: str = "person",
    config: dict = None,
) -> str | None:
    """
    Generate a fun, short title from an image using the LLM.

    Reads LLM config from the provided config dict (or from config.yaml
    if not supplied). The caller provides a dict of context variables
    that will be substituted into the event-specific prompt.

    Args:
        image_data: Raw image bytes.
        context: Dict of context variables to substitute into the prompt.
                 Keys depend on event_type (e.g. person_name, city, country,
                 then_year, now_year, gap, person_names, etc.).
        event_type: Type of event to select the appropriate prompt.
                   One of: 'person', 'then_and_now', 'trip_highlights',
                   'weekly_collage'. Defaults to 'person'.
        config: Application config dict (must contain 'llm' section with
                'url', 'model', 'api_key'). If omitted, loads config.yaml.

    Returns:
        A short, fun title string, or None if generation failed.
    """
    logger = logging.getLogger("immich-memories-notify")

    if config is None:
        from notify.config import load_config

        config = load_config()

    llm_config = config.get("llm", {})
    llm_url = llm_config.get("url", "")
    model = llm_config.get("model", "")
    api_key = llm_config.get("api_key", "")

    if not llm_url or not model:
        logger.debug("LLM title generation skipped: no url/model configured")
        return None

    # Load prompt based on event type
    prompt_path = EVENT_PROMPT_MAP.get(event_type)
    try:
        prompt = Path(prompt_path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning(f"Prompt file not found for event_type '{event_type}', using default")
        prompt = DEFAULT_TITLE_PROMPT

    # Replace all context placeholders in the prompt
    prompt = prompt.format(**context)

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                    },
                ],
            }
        ],
        "temperature": 0.7,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        api_url = f"{llm_url.rstrip('/')}/chat/completions"
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        result = response.json()
        title = result["choices"][0]["message"]["content"].strip()

        # Clean up quotes/wrapping
        title = title.strip("\"'")
        logger.info(f"LLM title ({event_type}): {title}")
        return title

    except Exception as e:
        logger.warning(f"LLM title generation failed: {e}")
        return None
