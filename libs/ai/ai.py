# libs/ai/ai.py

import json
import logging
import os
import time
from typing import Any, Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class IA:
    """
    Client wrapper for interacting with the Anthropic Claude API.

    Supports:
    - Single-turn completions
    - Multi-turn (agentic) conversations
    - Structured JSON extraction
    - Retry with exponential back-off
    - Token-limit enforcement per call
    """

    DEFAULT_MODEL = "claude-opus-4-5"
    DEFAULT_MAX_TOKENS = 4096
    DEFAULT_TEMPERATURE = 0.0
    MAX_RETRIES = 5
    INITIAL_RETRY_DELAY = 2.0  # seconds

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Anthropic API key must be provided via the 'api_key' argument "
                "or the ANTHROPIC_API_KEY environment variable."
            )

        self.model = model or os.getenv("ANTHROPIC_MODEL", self.DEFAULT_MODEL)
        self.max_tokens = max_tokens or int(
            os.getenv("ANTHROPIC_MAX_TOKENS", str(self.DEFAULT_MAX_TOKENS))
        )
        self.temperature = temperature if temperature is not None else float(
            os.getenv("ANTHROPIC_TEMPERATURE", str(self.DEFAULT_TEMPERATURE))
        )
        self.max_retries = max_retries

        self._client = anthropic.Anthropic(api_key=self.api_key)

        logger.info(
            "IA client initialised — model=%s max_tokens=%s temperature=%s",
            self.model,
            self.max_tokens,
            self.temperature,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def completar(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Send a single-turn completion request and return the text response.

        Args:
            prompt:      User-turn message content.
            system:      Optional system prompt.
            max_tokens:  Override instance-level max_tokens for this call.
            temperature: Override instance-level temperature for this call.

        Returns:
            The assistant's text response as a plain string.
        """
        messages = [{"role": "user", "content": prompt}]
        response = self._chamar_api(
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._extrair_texto(response)

    def completar_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Any:
        """
        Send a single-turn completion request expecting a JSON response.

        The method strips any accidental markdown fences before parsing.

        Args:
            prompt:      User-turn message content.
            system:      Optional system prompt.
            max_tokens:  Override instance-level max_tokens for this call.
            temperature: Override instance-level temperature for this call.

        Returns:
            Parsed Python object (dict / list) from the JSON response.

        Raises:
            ValueError: If the response cannot be decoded as valid JSON.
        """
        texto = self.completar(
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._parsear_json(texto)

    def conversar(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Send a multi-turn conversation and return the assistant's latest reply.

        Args:
            messages:    List of role/content dicts (alternating user/assistant).
            system:      Optional system prompt.
            max_tokens:  Override instance-level max_tokens for this call.
            temperature: Override instance-level temperature for this call.

        Returns:
            The assistant's latest text response.
        """
        response = self._chamar_api(
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._extrair_texto(response)

    def executar_agente(
        self,
        prompt_agente: str,
        contexto: Optional[str] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> dict:
        """
        Execute a structured agent turn.

        Combines an optional context string with the agent prompt, requests a
        JSON-structured response, and returns the parsed result together with
        metadata.

        Args:
            prompt_agente: The agent's task prompt (may include {contexto} placeholder).
            contexto:      Optional context string to inject into the prompt.
            system:        Optional system prompt.
            max_tokens:    Override instance-level max_tokens for this call.
            temperature:   Override instance-level temperature for this call.

        Returns:
            dict with keys:
                - "sucesso" (bool)
                - "resposta" (parsed JSON object or raw string on failure)
                - "erro" (str or None)
                - "modelo" (str)
                - "tokens_entrada" (int)
                - "tokens_saida" (int)
        """
        if contexto and "{contexto}" in prompt_agente:
            prompt_final = prompt_agente.replace("{contexto}", contexto)
        elif contexto:
            prompt_final = f"{contexto}\n\n{prompt_agente}"
        else:
            prompt_final = prompt_agente

        messages = [{"role": "user", "content": prompt_final}]

        try:
            response = self._chamar_api(
                messages=messages,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            texto = self._extrair_texto(response)
            resposta_json = self._parsear_json(texto)

            return {
                "sucesso": True,
                "resposta": resposta_json,
                "erro": None,
                "modelo": response.model,
                "tokens_entrada": response.usage.input_tokens,
                "tokens_saida": response.usage.output_tokens,
            }

        except json.JSONDecodeError as exc:
            logger.warning("Agente retornou JSON inválido: %s", exc)
            return {
                "sucesso": False,
                "resposta": texto if "texto" in dir() else "",
                "erro": f"JSON inválido: {exc}",
                "modelo": self.model,
                "tokens_entrada": 0,
                "tokens_saida": 0,
            }

        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Falha ao executar agente: %s", exc)
            return {
                "sucesso": False,
                "resposta": None,
                "erro": str(exc),
                "modelo": self.model,
                "tokens_entrada": 0,
                "tokens_saida": 0,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _chamar_api(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,