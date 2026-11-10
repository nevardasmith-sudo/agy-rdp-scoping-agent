"""Agent 1: Discovery & Industry Classifier for the RPD Transformation Engine."""

import os
import urllib.request
from html.parser import HTMLParser
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    class BaseModel:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    def Field(default: Any = None, **kwargs: Any) -> Any:  # type: ignore
        return default

try:
    from google.adk.agents import Agent
    from google.adk.models import Gemini
    from google.genai import types
except ImportError:  # pragma: no cover
    class Agent:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class Gemini:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class _TypesStub:
        class HttpRetryOptions:
            def __init__(self, **kwargs: Any) -> None:
                self.__dict__.update(kwargs)

    types = _TypesStub()  # type: ignore


class CustomerContextSchema(BaseModel):
    """Structured customer discovery context and classification schema."""

    customer_name: str = Field(description="Name of the customer organization")
    industry_vertical: str = Field(
        description=(
            "Primary industry vertical identified (e.g., Banking, Capital Markets, "
            "Insurance, Payments, Cross-FSI, Life Sciences, Healthcare, Retail, CPG, "
            "Travel & Hospitality, Manufacturing)"
        )
    )
    operating_model: str = Field(description="Core operating model details")
    friction_points: list[str] = Field(
        description="Key business and operational friction points extracted from sources"
    )


def ingest_customer_sources(
    urls: Optional[list[str]] = None,
    raw_docs: Optional[list[str]] = None,
    raw_text_fallback: Optional[str] = None,
    discovery_passages: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Ingest up to 5 Web URLs and up to 5 discovery documents, returning unified text context.

    Degrades gracefully to raw user-pasted text if web scraping encounters HTTP 403,
    HTTP 429, or connection errors.
    """
    combined_context: list[str] = []
    docs_to_process = raw_docs if raw_docs is not None else discovery_passages

    if urls:
        for url in urls[:5]:
            clean_url = str(url).strip()
            if not clean_url:
                continue
            try:
                headers = {"User-Agent": "EnterpriseAgent/1.0"}
                response = requests.get(clean_url, headers=headers, timeout=10)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, "html.parser")
                    text = " ".join([p.get_text(" ", strip=True) for p in soup.find_all("p")])
                    combined_context.append(
                        f"--- URL Content ({clean_url}) ---\n{text[:3000]}"
                    )
                elif response.status_code in (403, 429):
                    combined_context.append(
                        f"--- URL Fetch Blocked ({clean_url}): HTTP {response.status_code} "
                        "(Firewall/Anti-Scraping). Degrading gracefully to raw text fallback. ---"
                    )
                else:
                    combined_context.append(
                        f"--- URL Fetch Failed ({clean_url}): HTTP {response.status_code}. "
                        "Fallback to raw text context. ---"
                    )
            except requests.RequestException as e:
                combined_context.append(
                    f"--- URL Scraping Error ({clean_url}): {str(e)}. "
                    "Fallback to raw text context. ---"
                )
            except Exception as e:
                combined_context.append(
                    f"--- URL Scraping Error ({clean_url}): {str(e)}. "
                    "Fallback to raw text context. ---"
                )

    passage_count = 0
    if docs_to_process:
        for idx, doc_text in enumerate(docs_to_process[:5], 1):
            if doc_text and str(doc_text).strip():
                passage_count += 1
                combined_context.append(
                    f"--- Discovery Document {idx} ---\n{str(doc_text).strip()}"
                )

    if raw_text_fallback and str(raw_text_fallback).strip():
        passage_count += 1
        combined_context.append(
            f"--- Raw User-Pasted Fallback Context ---\n{str(raw_text_fallback).strip()}"
        )

    unified_text = (
        "\n\n".join(combined_context)
        if combined_context
        else "--- No external URLs or discovery documents provided. Provide raw text context. ---"
    )
    return {
        "url_count": len(urls[:5]) if urls else 0,
        "passage_count": passage_count,
        "combined_context": unified_text,
    }


discovery_agent = Agent(
    name="discovery_industry_classifier",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are Agent 1: Enterprise Discovery & Industry Classifier for the Google Cloud "
        "Repeatable, Portable, and Demoable (RPD) Transformation Engine.\n"
        "1. Use `ingest_customer_sources` to ingest up to 5 customer Web URLs and up to 5 "
        "discovery document text passages (or raw user-pasted text fallback when HTTP 403/429 "
        "or connection errors occur).\n"
        "2. Analyze the ingested context and output a structured JSON object strictly matching "
        "`CustomerContextSchema` with fields:\n"
        "   - `customer_name`: Name of the customer organization\n"
        "   - `industry_vertical`: Primary industry vertical identified\n"
        "   - `operating_model`: Core operating model details\n"
        "   - `friction_points`: List of key business and operational friction points\n"
        "3. CRITICAL GUARDRAIL: Synthesize the industry taxonomy, operating model, and friction "
        "points strictly and exclusively from the provided inputs. Do not invent external trivia, "
        "assumed capabilities, or unstated business challenges."
    ),
    output_schema=CustomerContextSchema,
    tools=[ingest_customer_sources],
)
