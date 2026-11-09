"""OpenAI-compatible chat client for an online host and a local vision server."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path

from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    OpenAI,
    Timeout,
)
from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat.chat_completion_content_part_image_param import (
    ChatCompletionContentPartImageParam,
    ImageURL,
)
from openai.types.chat.chat_completion_content_part_param import (
    ChatCompletionContentPartParam,
)
from openai.types.chat.chat_completion_content_part_text_param import (
    ChatCompletionContentPartTextParam,
)

from canvas_grader.config import DEFAULT_ONLINE_BASE_URL
from canvas_grader.render import tiny_test_jpeg
from canvas_grader.store import Settings

ImageSource = Path | bytes


@dataclass
class Part:
    """One block of a user message. Images are never placed in the system message."""

    text: str | None = None
    image: ImageSource | None = None


class LLMError(Exception):
    """A model call failed in a way the UI can show directly."""


@dataclass
class Endpoint:
    name: str
    base_url: str
    api_key: str
    model: str


@dataclass
class ConnectionTest:
    ok: bool
    message: str


def endpoint_for(settings: Settings, provider: str) -> Endpoint:
    if provider == "online":
        if not settings.online_api_key.strip():
            raise LLMError("No online API key yet. Add it on the Settings page.")
        model = settings.online_model.strip()
        if not model:
            raise LLMError("No online model name yet. Add it on the Settings page.")
        base_url = settings.online_base_url.strip() or DEFAULT_ONLINE_BASE_URL
        return Endpoint(
            name="Online model",
            base_url=base_url,
            api_key=settings.online_api_key.strip(),
            model=model,
        )
    if provider != "local":
        raise LLMError(f"Unknown model option: {provider}")
    model = settings.local_model.strip()
    if not model:
        raise LLMError("No local model name yet. Add it on the Settings page.")
    base_url = settings.local_base_url.strip()
    if not base_url:
        raise LLMError("No local address yet. Add it on the Settings page.")
    return Endpoint(
        name="Local model",
        base_url=base_url,
        api_key=settings.local_api_key.strip() or "local",
        model=model,
    )


def test_endpoint(endpoint: Endpoint) -> ConnectionTest:
    """List models, then send a generated JPEG. Student work is never used here."""
    notes: list[str] = []
    try:
        model_ids = list_model_ids(endpoint)
    except APIConnectionError:
        return ConnectionTest(
            False,
            f"Cannot reach {endpoint.base_url}. Check the address and that the server is running.",
        )
    except APITimeoutError:
        return ConnectionTest(
            False,
            f"Timed out connecting to {endpoint.base_url}. Check the address and that the server is running.",
        )
    except Exception as exc:
        notes.append(f"Could not list models: {_short(exc)}")
        model_ids = None
    if model_ids is not None:
        if endpoint.model in model_ids:
            notes.append(f"Found model {endpoint.model}.")
        else:
            preview = ", ".join(model_ids[:12]) or "(empty)"
            notes.append(
                f'"{endpoint.model}" is not in the list. The server currently has: {preview}'
            )
    try:
        reply = chat(
            endpoint,
            system="Reply with exactly the word ok and nothing else.",
            user_text="Reply with exactly the word ok.",
            images=[tiny_test_jpeg()],
            timeout=180,
        )
    except LLMError as exc:
        notes.append(
            f"Vision check failed: {exc}. This address or model may not accept images."
        )
        return ConnectionTest(False, "\n".join(notes))
    if "ok" in reply.lower():
        notes.append("Vision check passed.")
        return ConnectionTest(True, "\n".join(notes))
    notes.append(f"The model replied, but not with ok: {reply[:200]}")
    return ConnectionTest(False, "\n".join(notes))


def list_model_ids(endpoint: Endpoint) -> list[str]:
    client = _client(endpoint, timeout=30)
    listing = client.models.list()
    data = getattr(listing, "data", listing)
    identifiers: list[str] = []
    for item in data:
        identifier = getattr(item, "id", None)
        if identifier is None and isinstance(item, dict):
            identifier = item.get("id")
        if identifier:
            identifiers.append(str(identifier))
    return identifiers


def chat_json(
    endpoint: Endpoint,
    *,
    system: str,
    user_text: str = "",
    images: list[ImageSource] | None = None,
    parts: list[Part] | None = None,
) -> dict:
    """Ask for one JSON object and retry once if the reply is not valid JSON."""
    content_parts = _parts(user_text, images, parts)
    reply = chat(endpoint, system=system, parts=content_parts)
    try:
        return _extract_json(reply)
    except json.JSONDecodeError:
        reminder = Part(
            text=(
                "Your previous reply was not a single JSON object. "
                "Reply again with only the JSON object and no markdown.\n\nPrevious reply:\n"
                + reply[:2000]
            )
        )
        second = chat(endpoint, system=system, parts=[*content_parts, reminder])
        try:
            return _extract_json(second)
        except json.JSONDecodeError as exc:
            raise LLMError("The model did not return valid JSON.") from exc


def chat(
    endpoint: Endpoint,
    *,
    system: str,
    user_text: str = "",
    images: list[ImageSource] | None = None,
    parts: list[Part] | None = None,
    timeout: float = 600,
) -> str:
    content_parts = _parts(user_text, images, parts)
    try:
        return _complete(endpoint, system, content_parts, detail=True, timeout=timeout)
    except LLMError:
        raise
    except BadRequestError as exc:
        message = _short(exc).lower()
        if _has_image(content_parts) and "detail" in message:
            return _complete(
                endpoint, system, content_parts, detail=False, timeout=timeout
            )
        if "system" in message:
            merged = [Part(text=system), *content_parts]
            return _complete(endpoint, None, merged, detail=True, timeout=timeout)
        raise LLMError(_short(exc)) from exc
    except (APIConnectionError, APITimeoutError) as exc:
        raise LLMError(f"Cannot reach {endpoint.name}: {_short(exc)}") from exc
    except Exception as exc:
        raise LLMError(_short(exc)) from exc


def _complete(
    endpoint: Endpoint,
    system: str | None,
    parts: list[Part],
    *,
    detail: bool,
    timeout: float,
) -> str:
    messages: list[ChatCompletionMessageParam] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": _user_content(parts, detail)})
    client = _client(endpoint, timeout=timeout)
    try:
        response = client.chat.completions.create(
            model=endpoint.model, messages=messages
        )
    except APIConnectionError, APITimeoutError:
        response = client.chat.completions.create(
            model=endpoint.model, messages=messages
        )
    content = response.choices[0].message.content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            else:
                texts.append(str(getattr(part, "text", "") or ""))
        content = "".join(texts)
    if not content or not str(content).strip():
        raise LLMError(f"{endpoint.name} returned an empty reply.")
    return str(content)


def _parts(
    user_text: str, images: list[ImageSource] | None, parts: list[Part] | None
) -> list[Part]:
    if parts is not None:
        return parts
    content = [Part(text=user_text)]
    content.extend(Part(image=image) for image in images or [])
    return content


def _has_image(parts: list[Part]) -> bool:
    return any(part.image is not None for part in parts)


def _user_content(
    parts: list[Part], detail: bool
) -> str | list[ChatCompletionContentPartParam]:
    blocks: list[ChatCompletionContentPartParam] = []
    for part in parts:
        if part.text:
            text_block: ChatCompletionContentPartTextParam = {
                "type": "text",
                "text": part.text,
            }
            blocks.append(text_block)
        if part.image is not None:
            encoded = base64.b64encode(_jpeg_bytes(part.image)).decode("ascii")
            image_url: ImageURL = {"url": f"data:image/jpeg;base64,{encoded}"}
            if detail:
                # OpenAI accepts low, high, or auto. "original" is rejected by LM Studio.
                image_url["detail"] = "high"
            image_block: ChatCompletionContentPartImageParam = {
                "type": "image_url",
                "image_url": image_url,
            }
            blocks.append(image_block)
    if len(blocks) == 1 and blocks[0]["type"] == "text":
        return str(blocks[0]["text"])
    if not blocks:
        return ""
    return blocks


def _jpeg_bytes(image: ImageSource) -> bytes:
    if isinstance(image, bytes):
        return image
    return image.read_bytes()


def _client(endpoint: Endpoint, timeout: float) -> OpenAI:
    return OpenAI(
        api_key=endpoint.api_key,
        base_url=endpoint.base_url.rstrip("/"),
        timeout=Timeout(timeout, connect=30),
        max_retries=0,
    )


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise json.JSONDecodeError("Expected an object", cleaned, 0)
    return value


def _short(exc: Exception) -> str:
    """Keep the response body. Some servers put the reason only there."""
    text = str(exc).strip() or exc.__class__.__name__
    body = getattr(exc, "body", None)
    if body:
        extra = str(body).strip()
        if extra and extra not in text:
            text = f"{text}: {extra}"
    return text[:500]
