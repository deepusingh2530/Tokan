"""Canonical request model: one typed representation for every wire protocol.

Agents speak four wires (OpenAI chat, OpenAI Responses, Anthropic Messages,
Gemini generateContent) and each protocol historically did its own ad-hoc
projection of ``messages``/``input``/``contents``. This module is the single
place that understands all four:

- :func:`parse` turns a provider payload into a :class:`CanonicalRequest`
  that keeps structured content (images, tool calls, tool results) typed —
  never flattened — plus an extension container for provider fields Tokan
  does not model. Nothing is discarded: every input key lands in exactly one
  bucket (structural, param, or extension) and :meth:`CanonicalRequest.to_payload`
  reconstructs a byte-equal provider payload from those buckets.

- :meth:`CanonicalRequest.fingerprint_view` reproduces **exactly** the
  ``(messages, scope)`` pair ``pipeline.prepare()`` digests today, for each
  protocol. This is the backward-compatibility contract: when the pipeline
  adopts the canonical model (M3), existing cache entries keep matching —
  proven by parity tests, not convention.

Digest-scope de-volatilization (system-preamble scrubbing) stays in
``pipeline.devolatile``: the canonical model carries *original bytes*; only
the digest view is scrubbed, and that happens downstream of
``fingerprint_view()``.

Known reconstruction limitations (irrelevant while observe-only; re-verified
at M3 wire adoption): an explicitly-empty ``tools: []`` array or an explicit
``tool_choice: null`` is dropped (presence without content carries no
digest-relevant information), and Gemini tool *groups* with keys beside
``functionDeclarations`` lose those group-level keys (per-declaration extras
are preserved).

Stdlib only, no provider SDKs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ContentPart",
    "CanonicalMessage",
    "ToolDef",
    "CanonicalRequest",
    "parse",
    "to_payload",
    "DEFAULT_SCOPE_KEYS",
]

#: Matches pipeline.OptimizationPipeline.scope_keys (the digest-scope fields).
#: fingerprint_view(scope_keys=...) accepts a custom tuple for pipelines
#: configured with different scope keys; parity tests pin the default.
DEFAULT_SCOPE_KEYS: Tuple[str, ...] = ("model", "temperature", "top_p", "user")

# Generation-affecting params preserved verbatim per protocol. Everything not
# structural and not listed here lands in `extensions` and is re-emitted —
# buckets affect what the governor can *reason about*, never what survives.
_CHAT_PARAMS = (
    "temperature", "top_p", "frequency_penalty", "presence_penalty", "max_tokens",
    "max_completion_tokens", "stop", "n", "stream", "stream_options",
    "response_format", "logit_bias", "logprobs", "top_logprobs", "parallel_tool_calls",
    "user",
)
_ANTHROPIC_PARAMS = (
    "temperature", "top_p", "top_k", "max_tokens", "stop_sequences", "stream",
)
_GEMINI_PARAMS = (
    "generationConfig", "candidateCount", "topK", "temperature", "maxOutputTokens",
    "safeSearchConfig", "thinkingConfig",
)
_RESPONSES_PARAMS = (
    "temperature", "top_p", "max_output_tokens", "stream", "stream_options",
    "parallel_tool_calls", "truncation", "reasoning", "text", "store", "service_tier",
)


# --------------------------------------------------------------------- parts
@dataclass(frozen=True)
class ContentPart:
    """One typed piece of message content.

    ``kind`` is one of: ``text``, ``image``, ``tool_call``, ``tool_result``,
    ``unknown``. Unknown parts carry the original wire block in
    ``extensions`` and are re-emitted verbatim — Tokan never invents structure
    it does not understand.
    """

    kind: str
    text: str = ""
    media_type: str = ""
    data: str = ""
    tool_call_id: str = ""
    name: str = ""
    extensions: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalMessage:
    """One conversation message with structured (non-flattened) content.

    ``content_shape`` records how the wire carried ``content`` (``"str"``,
    ``"list"``, ``"null"``) so reconstruction reproduces it exactly.
    ``extensions`` holds wire keys Tokan does not model (``name``,
    ``tool_call_id``, ``cache_control``, ...) — re-emitted verbatim.
    ``origin`` records the wire item type where the protocol is item-typed
    (Responses API: ``message`` / ``function_call`` / ...), else ``""``.
    """

    role: str
    parts: Tuple[ContentPart, ...] = ()
    content_shape: str = "str"
    extensions: Dict[str, Any] = field(default_factory=dict)
    origin: str = ""

    def text(self) -> str:
        """Joined text parts — the projection the digest and state use.

        Tool calls and images contribute no text (their payloads live in
        typed fields), matching the historical chat projection.
        """
        return "\n".join(p.text for p in self.parts if p.kind == "text" and p.text)


@dataclass(frozen=True)
class ToolDef:
    """A tool/function definition with its provider-specific shape preserved."""

    name: str
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    #: Provider wrapper keys (openai: extras beside ``type``/``function`` and
    #: inside ``function``; anthropic: keys beside name/description/input_schema).
    extensions: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalRequest:
    """Normalized request that can reconstruct a valid provider payload.

    ``provider``/``protocol`` name the wire it came from (``openai``/``chat``,
    ``openai``/``responses``, ``anthropic``/``messages``, ``gemini``/
    ``generate_content``). ``system`` holds the Anthropic top-level system
    field (or Responses ``instructions``) verbatim — structural for those
    protocols, not a message.
    """

    provider: str
    protocol: str
    model: str = ""
    messages: Tuple[CanonicalMessage, ...] = ()
    tools: Tuple[ToolDef, ...] = ()
    tool_choice: Any = None
    params: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)
    system: Any = None

    # ---------------------------------------------------------- projections
    def fingerprint_view(
        self,
        scope_keys: Sequence[str] = DEFAULT_SCOPE_KEYS,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Exactly the ``(messages, scope)`` pair ``pipeline.prepare()`` digests.

        Byte-parity with ``prepare()`` is contract-tested per protocol; the
        pipeline (M3) will consume this view so cache digests never change.
        """
        if self.protocol == "responses":
            messages = self._responses_projection()
        elif self.protocol == "generate_content":
            messages = [
                {
                    "role": m.role,
                    "content": "\n".join(p.text for p in m.parts if p.kind == "text"),
                }
                for m in self.messages
                if any(p.kind == "text" for p in m.parts)
            ]
        else:  # chat / messages wires: raw-equivalent message dicts
            messages = [message_to_wire(m, self.protocol) for m in self.messages]

        scope: Dict[str, Any] = {}
        for key in scope_keys:
            if key == "model":
                value: Any = self.model or None
            elif key in self.params:
                value = self.params[key]
            else:
                value = None
            if value is not None:
                scope[key] = value
        if self.protocol == "messages" and self.system is not None:
            scope["system"] = self.system
        return messages, scope

    def _responses_projection(self) -> List[Dict[str, Any]]:
        """Replicates pipeline.responses_input_to_messages (parity-tested).

        Text-bearing ``message`` items project to role/content; tool calls
        project as ``name(arguments)`` assistant messages; tool outputs as
        ``tool`` role; ``instructions`` (in ``system``) and reasoning items
        are excluded exactly like the pipeline does.
        """
        out: List[Dict[str, Any]] = []
        for m in self.messages:
            if m.origin == "function_call":
                out.append({
                    "role": "assistant",
                    "content": f"{m.extensions.get('name', '')}({m.extensions.get('arguments', '')})",
                })
            elif m.origin == "function_call_output":
                value: Any = m.extensions.get("output", "")
                if isinstance(value, list):
                    value = "\n".join(
                        p.get("text", "") for p in value if isinstance(p, dict)
                    )
                out.append({"role": "tool", "content": str(value or "")})
            elif m.origin == "reasoning":
                continue  # per-turn scratchpad, not conversation
            else:
                out.append({"role": m.role or "user", "content": m.text()})
        return out

    def newest_text_message(self) -> Optional[Tuple[str, str]]:
        """Newest ``(role, text)`` carrying message content, or None.

        Text-bearing messages only: tool-call-only assistant turns and
        tool-output items contribute no text here (callers that need those
        read ``parts`` directly).
        """
        for m in reversed(self.messages):
            text = m.text()
            if text:
                return m.role, text
        return None

    # ------------------------------------------------------------ wire out
    def to_payload(self) -> Dict[str, Any]:
        """Reconstruct the provider payload (round-trip tested per protocol)."""
        if self.protocol == "responses":
            return _to_responses(self)
        if self.protocol == "messages":
            return _to_anthropic(self)
        if self.protocol == "generate_content":
            return _to_gemini(self)
        return _to_openai_chat(self)


# ------------------------------------------------------------------- parse
def parse(payload: Mapping[str, Any], *, protocol: Optional[str] = None) -> CanonicalRequest:
    """Parse a provider payload into a :class:`CanonicalRequest`.

    ``protocol`` is ``"openai"`` (chat), ``"responses"``, ``"anthropic"``,
    ``"gemini"`` — or inferred: ``contents`` → gemini, ``input`` → responses,
    else chat/messages per the ``protocol`` argument (default openai chat).
    """
    if protocol is None:
        if "contents" in payload:
            protocol = "generate_content"
        elif "input" in payload:
            protocol = "responses"
        else:
            protocol = "chat"
    if protocol in ("openai", "chat"):
        return _parse_openai_chat(payload)
    if protocol == "responses":
        return _parse_responses(payload)
    if protocol in ("anthropic", "messages"):
        return _parse_anthropic(payload)
    if protocol in ("gemini", "generate_content"):
        return _parse_gemini(payload)
    raise ValueError(f"unknown protocol {protocol!r}")


def to_payload(request: CanonicalRequest) -> Dict[str, Any]:
    """Reconstruct the provider payload for ``request``'s protocol."""
    return request.to_payload()


# ---------------------------------------------------------- openai chat
def _parse_openai_chat(payload: Mapping[str, Any]) -> CanonicalRequest:
    known = ("messages", "model", "tools", "tool_choice")
    params = {k: payload[k] for k in _CHAT_PARAMS if k in payload}
    extensions = {k: v for k, v in payload.items() if k not in known and k not in params}
    return CanonicalRequest(
        provider="openai",
        protocol="chat",
        model=str(payload.get("model", "")),
        messages=tuple(_parse_chat_message(m) for m in payload.get("messages") or []),
        tools=tuple(_parse_openai_tool(t) for t in payload.get("tools") or []),
        tool_choice=payload.get("tool_choice"),
        params=params,
        extensions=extensions,
    )


def _parse_chat_message(raw: Any) -> CanonicalMessage:
    if not isinstance(raw, dict):
        return CanonicalMessage(role="", content_shape="str")
    known = ("role", "content", "tool_calls")
    extensions = {k: v for k, v in raw.items() if k not in known}
    content = raw.get("content")
    parts: List[ContentPart] = []
    if isinstance(content, str):
        shape = "str"
        if content:
            parts.append(ContentPart(kind="text", text=content))
    elif isinstance(content, list):
        shape = "list"
        parts.extend(_parse_chat_part(p) for p in content)
    else:
        shape = "null"
    for call in raw.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        call_ext = {k: v for k, v in call.items() if k not in ("id", "type", "function")}
        if "function" in call and function:
            call_ext["function"] = {
                k: v for k, v in function.items() if k not in ("name", "arguments")
            }
        parts.append(
            ContentPart(
                kind="tool_call",
                tool_call_id=str(call.get("id", "")),
                name=str(function.get("name", "")),
                data=str(function.get("arguments", "")),
                extensions=call_ext,
            )
        )
    return CanonicalMessage(
        role=str(raw.get("role", "")),
        parts=tuple(parts),
        content_shape=shape,
        extensions=extensions,
    )


def _parse_chat_part(raw: Any) -> ContentPart:
    if not isinstance(raw, dict):
        return ContentPart(kind="unknown", extensions={"__part": raw})
    ptype = raw.get("type")
    extras = {k: v for k, v in raw.items() if k not in ("type", "text")}
    if ptype == "text" and isinstance(raw.get("text"), str):
        return ContentPart(kind="text", text=raw["text"], extensions=extras)
    if ptype == "image_url":
        url = raw.get("image_url")
        url = url.get("url", "") if isinstance(url, dict) else str(url or "")
        return ContentPart(kind="image", data=url, extensions=extras)
    # Unknown block types (input_audio, file, ...) keep the raw block verbatim.
    return ContentPart(kind="unknown", extensions={"__part": raw})


def _parse_openai_tool(raw: Any) -> ToolDef:
    if not isinstance(raw, dict):
        return ToolDef(name="", extensions={"__tool": raw})
    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    wrapper = {k: v for k, v in raw.items() if k not in ("type", "function")}
    fn_ext = {k: v for k, v in function.items() if k not in ("name", "description", "parameters")}
    if fn_ext:
        wrapper["function"] = fn_ext
    # An explicitly-empty "parameters": {} is meaningful (schema present but
    # no args) — remember presence so reconstruction doesn't drop it.
    if "parameters" in function and not function.get("parameters"):
        wrapper["__empty_params"] = True
    return ToolDef(
        name=str(function.get("name", raw.get("name", ""))),
        description=str(function.get("description", "")),
        parameters=function.get("parameters") or {},
        extensions=wrapper,
    )


def _tool_to_openai_wire(tool: ToolDef) -> Dict[str, Any]:
    wrapper = dict(tool.extensions)
    empty_params = wrapper.pop("__empty_params", False)
    function = dict(wrapper.pop("function", {}))
    function.update({"name": tool.name, "description": tool.description})
    if tool.parameters or empty_params:
        function["parameters"] = tool.parameters
    entry: Dict[str, Any] = {"type": "function", "function": function}
    entry.update(wrapper)
    return entry


def _to_openai_chat(req: CanonicalRequest) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(req.extensions)
    out["model"] = req.model
    out["messages"] = [message_to_wire(m, "chat") for m in req.messages]
    if req.tools:
        out["tools"] = [_tool_to_openai_wire(t) for t in req.tools]
    if req.tool_choice is not None:
        out["tool_choice"] = req.tool_choice
    out.update(req.params)
    return out


# --------------------------------------------------------- openai responses
def _parse_responses(payload: Mapping[str, Any]) -> CanonicalRequest:
    known = ("input", "model", "tools", "tool_choice", "instructions")
    params = {k: payload[k] for k in _RESPONSES_PARAMS if k in payload}
    extensions = {k: v for k, v in payload.items() if k not in known and k not in params}
    input_value = payload.get("input")
    messages: List[CanonicalMessage] = []
    if isinstance(input_value, str):
        if input_value:
            messages.append(CanonicalMessage(
                role="user", parts=(ContentPart(kind="text", text=input_value),), origin="message",
            ))
    elif isinstance(input_value, list):
        messages.extend(
            _parse_responses_item(item) for item in input_value if isinstance(item, dict)
        )
    return CanonicalRequest(
        provider="openai",
        protocol="responses",
        model=str(payload.get("model", "")),
        messages=tuple(messages),
        tools=tuple(_parse_openai_tool(t) for t in payload.get("tools") or []),
        tool_choice=payload.get("tool_choice"),
        params=params,
        extensions=extensions,
        system=payload.get("instructions"),
    )


def _parse_responses_item(item: Mapping[str, Any]) -> CanonicalMessage:
    kind = item.get("type")
    known_item_keys = ("type", "role", "content", "name", "arguments", "output", "id", "call_id")
    extensions = {k: v for k, v in item.items() if k not in known_item_keys}
    if "id" in item:
        extensions["id"] = item["id"]
    if "call_id" in item:
        extensions["call_id"] = item["call_id"]
    parts: List[ContentPart] = []
    content = item.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("input_text", "output_text", "text") and isinstance(block.get("text"), str):
                block_ext = {k: v for k, v in block.items() if k not in ("type", "text")}
                parts.append(ContentPart(kind="text", text=block["text"], extensions={"__type": btype, **block_ext}))
            elif btype in ("input_image", "image_url"):
                url = block.get("image_url") or block.get("file_id") or ""
                if isinstance(url, dict):
                    url = url.get("url", "")
                block_ext = {k: v for k, v in block.items() if k not in ("type", "image_url", "file_id")}
                parts.append(ContentPart(kind="image", data=str(url), extensions={"__type": btype, **block_ext}))
            else:
                parts.append(ContentPart(kind="unknown", extensions={"__part": block}))
    elif isinstance(content, str) and content:
        parts.append(ContentPart(kind="text", text=content))
    if kind == "function_call":
        extensions["name"] = item.get("name", "")
        extensions["arguments"] = item.get("arguments", "")
    if kind == "function_call_output":
        extensions["output"] = item.get("output")
    if kind == "reasoning":
        extensions["__reasoning"] = item.get("summary", [])
    if kind in ("function_call", "reasoning"):
        role = "assistant"
    elif kind == "function_call_output":
        role = "tool"
    else:
        role = str(item.get("role", "")) or "user"
    return CanonicalMessage(
        role=role,
        parts=tuple(parts),
        content_shape="list" if isinstance(content, list) else "str",
        extensions=extensions,
        origin=str(kind or ("message" if item.get("role") else "")),
    )


def _to_responses(req: CanonicalRequest) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(req.extensions)
    out["model"] = req.model
    if req.system is not None:
        out["instructions"] = req.system
    items: List[Dict[str, Any]] = []
    for m in req.messages:
        if m.origin in ("function_call", "function_call_output"):
            entry: Dict[str, Any] = {"type": m.origin}
            if m.origin == "function_call":
                entry["name"] = m.extensions.get("name", "")
                entry["arguments"] = m.extensions.get("arguments", "")
            else:
                entry["output"] = m.extensions.get("output")
            for key in ("call_id", "id"):
                if m.extensions.get(key):
                    entry[key] = m.extensions[key]
            entry.update({
                k: v for k, v in m.extensions.items()
                if k not in ("name", "arguments", "output", "call_id", "id", "__reasoning")
            })
            items.append(entry)
            continue
        if m.origin == "reasoning":
            entry = {"type": "reasoning"}
            if "__reasoning" in m.extensions:
                entry["summary"] = m.extensions["__reasoning"]
            entry.update({k: v for k, v in m.extensions.items() if k != "__reasoning"})
            items.append(entry)
            continue
        entry = {"type": "message"}
        if m.role:
            entry["role"] = m.role
        blocks = [_part_to_wire(p, "responses") for p in m.parts]
        entry["content"] = blocks if m.content_shape == "list" else m.text()
        entry.update({k: v for k, v in m.extensions.items()})
        items.append(entry)
    out["input"] = items
    if req.tools:
        out["tools"] = [_tool_to_openai_wire(t) for t in req.tools]
    if req.tool_choice is not None:
        out["tool_choice"] = req.tool_choice
    out.update(req.params)
    return out


# --------------------------------------------------------------- anthropic
def _parse_anthropic(payload: Mapping[str, Any]) -> CanonicalRequest:
    known = ("messages", "model", "system", "tools", "tool_choice")
    params = {k: payload[k] for k in _ANTHROPIC_PARAMS if k in payload}
    extensions = {k: v for k, v in payload.items() if k not in known and k not in params}
    return CanonicalRequest(
        provider="anthropic",
        protocol="messages",
        model=str(payload.get("model", "")),
        messages=tuple(_parse_anthropic_message(m) for m in payload.get("messages") or []),
        tools=tuple(_parse_anthropic_tool(t) for t in payload.get("tools") or []),
        tool_choice=payload.get("tool_choice"),
        params=params,
        extensions=extensions,
        system=payload.get("system"),
    )


def _parse_anthropic_message(raw: Any) -> CanonicalMessage:
    if not isinstance(raw, dict):
        return CanonicalMessage(role="", content_shape="str")
    known = ("role", "content")
    extensions = {k: v for k, v in raw.items() if k not in known}
    content = raw.get("content")
    parts: List[ContentPart] = []
    if isinstance(content, str):
        shape = "str"
        if content:
            parts.append(ContentPart(kind="text", text=content))
    elif isinstance(content, list):
        shape = "list"
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                block_ext = {k: v for k, v in block.items() if k not in ("type", "text")}
                parts.append(ContentPart(kind="text", text=block["text"], extensions=block_ext))
            elif btype == "image":
                source = block.get("source") if isinstance(block.get("source"), dict) else {}
                block_ext = {k: v for k, v in block.items() if k not in ("type", "source")}
                parts.append(ContentPart(
                    kind="image",
                    media_type=str(source.get("media_type", "")),
                    data=str(source.get("data", "")),
                    extensions=block_ext,
                ))
            elif btype == "tool_use":
                block_ext = {k: v for k, v in block.items() if k not in ("type", "id", "name", "input")}
                parts.append(ContentPart(
                    kind="tool_call",
                    tool_call_id=str(block.get("id", "")),
                    name=str(block.get("name", "")),
                    data=json.dumps(block.get("input", ""), sort_keys=True, default=str),
                    extensions=block_ext,
                ))
            elif btype == "tool_result":
                raw_content = block.get("content")
                text = ""
                if isinstance(raw_content, str):
                    text = raw_content
                elif isinstance(raw_content, list):
                    text = "\n".join(
                        b.get("text", "") for b in raw_content
                        if isinstance(b, dict) and isinstance(b.get("text"), str)
                    )
                block_ext = {k: v for k, v in block.items() if k not in ("type", "tool_use_id", "content")}
                parts.append(ContentPart(
                    kind="tool_result",
                    tool_call_id=str(block.get("tool_use_id", "")),
                    text=text,
                    extensions=block_ext,
                ))
            else:
                parts.append(ContentPart(kind="unknown", extensions={"__part": block}))
    else:
        shape = "null"
    return CanonicalMessage(
        role=str(raw.get("role", "")), parts=tuple(parts), content_shape=shape, extensions=extensions,
    )


def _parse_anthropic_tool(raw: Any) -> ToolDef:
    if not isinstance(raw, dict):
        return ToolDef(name="", extensions={"__tool": raw})
    known = ("name", "description", "input_schema")
    extensions = {k: v for k, v in raw.items() if k not in known}
    if "input_schema" in raw and not raw.get("input_schema"):
        extensions["__empty_params"] = True
    return ToolDef(
        name=str(raw.get("name", "")),
        description=str(raw.get("description", "")),
        parameters=raw.get("input_schema") or {},
        extensions=extensions,
    )


def _to_anthropic(req: CanonicalRequest) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(req.extensions)
    out["model"] = req.model
    out["messages"] = [message_to_wire(m, "anthropic") for m in req.messages]
    if req.system is not None:
        out["system"] = req.system
    if req.tools:
        tools = []
        for tool in req.tools:
            entry = dict(tool.extensions)
            empty_params = entry.pop("__empty_params", False)
            entry.update({"name": tool.name, "description": tool.description})
            if tool.parameters or empty_params:
                entry["input_schema"] = tool.parameters
            tools.append(entry)
        out["tools"] = tools
    if req.tool_choice is not None:
        out["tool_choice"] = req.tool_choice
    out.update(req.params)
    return out


# ------------------------------------------------------------------- gemini
def _parse_gemini(payload: Mapping[str, Any]) -> CanonicalRequest:
    known = ("contents", "model", "tools")
    params = {k: payload[k] for k in _GEMINI_PARAMS if k in payload}
    extensions = {k: v for k, v in payload.items() if k not in known and k not in params}
    tools: List[ToolDef] = []
    for tool_group in payload.get("tools") or []:
        if isinstance(tool_group, dict) and "functionDeclarations" in tool_group:
            for decl in tool_group.get("functionDeclarations") or []:
                if isinstance(decl, dict):
                    known_decl = ("name", "description", "parameters")
                    tools.append(ToolDef(
                        name=str(decl.get("name", "")),
                        description=str(decl.get("description", "")),
                        parameters=decl.get("parameters") or {},
                        extensions={k: v for k, v in decl.items() if k not in known_decl},
                    ))
        elif isinstance(tool_group, dict):
            # google_search / code_execution style single-tool groups: kept
            # verbatim in extensions below (see module limitations note).
            extensions.setdefault("__tool_groups", []).append(tool_group)
        else:
            extensions.setdefault("__tool_groups", []).append(tool_group)
    messages: List[CanonicalMessage] = []
    for content in payload.get("contents") or []:
        if not isinstance(content, dict):
            continue
        known_c = ("role", "parts")
        parts = tuple(_parse_gemini_part(p) for p in content.get("parts") or [])
        messages.append(CanonicalMessage(
            role=str(content.get("role", "user")),
            parts=parts,
            content_shape="list",
            extensions={k: v for k, v in content.items() if k not in known_c},
        ))
    return CanonicalRequest(
        provider="gemini",
        protocol="generate_content",
        model=str(payload.get("model", "")),
        messages=tuple(messages),
        tools=tuple(tools),
        params=params,
        extensions=extensions,
    )


def _parse_gemini_part(raw: Any) -> ContentPart:
    if not isinstance(raw, dict):
        return ContentPart(kind="unknown", extensions={"__part": raw})
    if isinstance(raw.get("text"), str):
        ext = {k: v for k, v in raw.items() if k != "text"}
        return ContentPart(kind="text", text=raw["text"], extensions=ext)
    if "inline_data" in raw:
        inline = raw.get("inline_data") if isinstance(raw.get("inline_data"), dict) else {}
        ext = {k: v for k, v in raw.items() if k != "inline_data"}
        return ContentPart(
            kind="image",
            media_type=str(inline.get("mime_type", "")),
            data=str(inline.get("data", "")),
            extensions=ext,
        )
    if "functionCall" in raw:
        call = raw.get("functionCall") if isinstance(raw.get("functionCall"), dict) else {}
        ext = {k: v for k, v in raw.items() if k != "functionCall"}
        return ContentPart(
            kind="tool_call",
            name=str(call.get("name", "")),
            data=json.dumps(call.get("args", ""), sort_keys=True, default=str),
            extensions=ext,
        )
    if "functionResponse" in raw:
        response = raw.get("functionResponse") if isinstance(raw.get("functionResponse"), dict) else {}
        ext = {k: v for k, v in raw.items() if k != "functionResponse"}
        return ContentPart(
            kind="tool_result",
            name=str(response.get("name", "")),
            data=json.dumps(response.get("response", ""), sort_keys=True, default=str),
            extensions=ext,
        )
    return ContentPart(kind="unknown", extensions={"__part": raw})


def _to_gemini(req: CanonicalRequest) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(req.extensions)
    tool_groups = out.pop("__tool_groups", [])
    out["model"] = req.model
    contents = []
    for m in req.messages:
        entry: Dict[str, Any] = dict(m.extensions)
        entry["role"] = m.role
        entry["parts"] = [_part_to_wire(p, "generate_content") for p in m.parts]
        contents.append(entry)
    out["contents"] = contents
    groups: List[Dict[str, Any]] = list(tool_groups)
    if req.tools:
        declarations = []
        for tool in req.tools:
            decl = dict(tool.extensions)
            decl.update({"name": tool.name, "description": tool.description})
            if tool.parameters:
                decl["parameters"] = tool.parameters
            declarations.append(decl)
        groups.append({"functionDeclarations": declarations})
    if groups:
        out["tools"] = groups
    out.update(req.params)
    return out


# ----------------------------------------------------------- wire rebuild
def _part_to_wire(part: ContentPart, protocol: str) -> Dict[str, Any]:
    """Rebuild one content block in ``protocol``'s wire shape.

    Chat/Anthropic/Responses blocks carry a ``type`` discriminator; Gemini
    blocks are bare ({``text``} / {``inline_data``} / ...) — adding a type
    key there would corrupt the round trip.
    """
    if part.kind == "text":
        block: Dict[str, Any] = {
            k: v for k, v in part.extensions.items() if k != "__type"
        }
        if "__type" in part.extensions:
            block["type"] = part.extensions["__type"]
        elif protocol != "generate_content":
            block.setdefault("type", "text")
        block["text"] = part.text
        return block
    if part.kind == "image":
        block = {k: v for k, v in part.extensions.items() if k != "__type"}
        if protocol == "generate_content":
            if "inline_data" not in block and part.data:
                block["inline_data"] = {"mime_type": part.media_type, "data": part.data}
            return block
        ptype = part.extensions.get("__type")
        if ptype:
            block["type"] = ptype
            if ptype == "image_url":
                block.setdefault("image_url", {"url": part.data})
            return block
        if protocol == "messages":  # anthropic source block
            block.setdefault("type", "image")
            block.setdefault("source", {
                "type": "base64", "media_type": part.media_type, "data": part.data,
            })
        else:  # openai chat / responses
            block.setdefault("type", "image_url")
            block.setdefault("image_url", {"url": part.data})
        return block
    if part.kind == "tool_call":
        if protocol == "generate_content":
            block = dict(part.extensions)
            block.setdefault("functionCall", {
                "name": part.name, "args": json.loads(part.data) if part.data else {},
            })
            return block
        if protocol == "messages":  # anthropic tool_use block
            block = dict(part.extensions)
            block.update({
                "type": "tool_use",
                "id": part.tool_call_id,
                "name": part.name,
                "input": json.loads(part.data) if part.data else {},
            })
            return block
        return dict(part.extensions)  # chat: tool_calls live beside content
    if part.kind == "tool_result":
        if protocol == "generate_content":
            block = dict(part.extensions)
            block.setdefault("functionResponse", {
                "name": part.name, "response": json.loads(part.data) if part.data else {},
            })
            return block
        if protocol == "messages":  # anthropic tool_result block
            block = dict(part.extensions)
            block.update({
                "type": "tool_result",
                "tool_use_id": part.tool_call_id,
                "content": part.text,
            })
            return block
        return dict(part.extensions)
    # unknown part: original wire block verbatim
    raw = part.extensions.get("__part", part.extensions)
    return dict(raw) if isinstance(raw, dict) else {"type": "text", "text": str(raw)}


def message_to_wire(message: CanonicalMessage, protocol: str) -> Dict[str, Any]:
    """Rebuild one message dict for ``protocol``'s wire (round-trip tested)."""
    out: Dict[str, Any] = dict(message.extensions)
    out["role"] = message.role
    text_blocks = [p for p in message.parts if p.kind in ("text", "image", "unknown")]
    tool_calls = [p for p in message.parts if p.kind == "tool_call"]
    tool_results = [p for p in message.parts if p.kind == "tool_result"]

    if protocol in ("messages", "anthropic"):  # anthropic
        if message.content_shape == "str":
            out["content"] = message.text()
        else:
            blocks: List[Dict[str, Any]] = []
            for part in message.parts:
                if part.kind == "tool_call":
                    block = dict(part.extensions)
                    block.update({
                        "type": "tool_use",
                        "id": part.tool_call_id,
                        "name": part.name,
                        "input": json.loads(part.data) if part.data else {},
                    })
                    blocks.append(block)
                elif part.kind == "tool_result":
                    block = dict(part.extensions)
                    block.update({
                        "type": "tool_result",
                        "tool_use_id": part.tool_call_id,
                        "content": part.text,
                    })
                    blocks.append(block)
                elif part.kind == "image":
                    source = {
                        "type": "base64",
                        "media_type": part.media_type,
                        "data": part.data,
                    }
                    block = dict(part.extensions)
                    block.update({"type": "image", "source": source})
                    blocks.append(block)
                else:
                    blocks.append(_part_to_wire(part, protocol))
            out["content"] = blocks
        return out

    # openai chat (and default)
    if tool_calls:
        calls = []
        for part in tool_calls:
            function: Dict[str, Any] = dict(part.extensions.get("function", {}))
            function.update({"name": part.name, "arguments": part.data})
            call: Dict[str, Any] = {"type": "function", "function": function}
            if part.tool_call_id:
                call["id"] = part.tool_call_id
            call.update({
                k: v for k, v in part.extensions.items() if k not in ("function", "id")
            })
            calls.append(call)
        out["tool_calls"] = calls
    if message.content_shape == "str":
        out["content"] = message.text()
    elif message.content_shape == "null":
        out["content"] = None
    elif tool_results and not text_blocks:
        # chat tool-result messages carry content as a plain string
        out["content"] = "\n".join(p.text for p in tool_results)
    else:
        out["content"] = [_part_to_wire(p, protocol) for p in text_blocks]
    return out
