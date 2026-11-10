"""Agent 1: Discovery & Industry Classifier for the RPD Transformation Engine."""

import ipaddress
import os
import socket
import urllib.request
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urlparse

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


class IngestSourcesInput(BaseModel):
    """Input validation contract for `ingest_customer_sources`."""

    urls: list[str] = Field(
        default_factory=list,
        description="Up to 5 public HTTP/HTTPS customer URLs to scrape",
    )
    raw_docs: list[str] = Field(
        default_factory=list,
        description="Up to 5 customer discovery document text passages",
    )
    raw_text_fallback: Optional[str] = Field(
        default=None,
        description="Optional raw user-pasted fallback text if web scraping fails",
    )


class IngestSourcesOutput(BaseModel):
    """Output contract for `ingest_customer_sources`."""

    url_count: int = Field(description="Number of URLs processed (up to 5)")
    blocked_ssrf_urls: list[str] = Field(
        default_factory=list,
        description="URLs rejected by SSRF/private-IP security validation",
    )
    passage_count: int = Field(description="Number of discovery text passages ingested")
    pii_redactions_applied: int = Field(
        default=0,
        description="Number of PII or secret occurrences redacted from ingested sources",
    )
    combined_context: str = Field(description="PII-scrubbed unified customer context string")


def _is_safe_public_url(url: str) -> bool:
    """Validates URL scheme and blocks SSRF to loopback, link-local, or private IP ranges."""
    try:
        parsed = urlparse(str(url).strip())
    except Exception:
        return False

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False

    hostname = parsed.hostname.lower().strip()
    if hostname in (
        "localhost",
        "metadata.google.internal",
        "metadata",
        "169.254.169.254",
        "0.0.0.0",
    ) or hostname.endswith(".internal") or hostname.endswith(".local"):
        return False

    # Check if literal IP address is non-public
    try:
        literal_ip = ipaddress.ip_address(hostname)
        if (
            literal_ip.is_private
            or literal_ip.is_loopback
            or literal_ip.is_link_local
            or literal_ip.is_multicast
            or literal_ip.is_reserved
            or literal_ip.is_unspecified
        ):
            return False
    except ValueError:
        pass

    # Resolve hostname and verify all resolved A/AAAA records are public global IPs
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in addr_info:
            ip_obj = ipaddress.ip_address(sockaddr[0])
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                return False
    except socket.gaierror:
        # Allow graceful HTTP handling downstream if DNS is temporarily unreachable offline,
        # provided it is not a forbidden internal hostname or literal private IP.
        return True
    except Exception:
        return False

    return True


def ingest_customer_sources(
    urls: Optional[list[str]] = None,
    raw_docs: Optional[list[str]] = None,
    raw_text_fallback: Optional[str] = None,
    discovery_passages: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Ingest up to 5 Web URLs and up to 5 discovery documents, returning unified text context.

    Enforces SSRF protection against private/loopback/metadata IPs and degrades gracefully
    to raw user-pasted text if web scraping encounters HTTP 403, HTTP 429, or connection errors.

    Args:
        urls: Optional list of up to 5 public HTTP/HTTPS URLs to fetch.
        raw_docs: Optional list of up to 5 discovery text documents.
        raw_text_fallback: Optional fallback text supplied directly by the user.
        discovery_passages: Alias for `raw_docs` for backward compatibility.

    Returns:
        Dictionary conforming to `IngestSourcesOutput` with `url_count`, `blocked_ssrf_urls`,
        `passage_count`, and `combined_context`.
    """
    docs_input = raw_docs if raw_docs is not None else (discovery_passages or [])
    validated = IngestSourcesInput(
        urls=[str(u).strip() for u in (urls or []) if str(u).strip()][:5],
        raw_docs=[str(d).strip() for d in docs_input if str(d).strip()][:5],
        raw_text_fallback=str(raw_text_fallback).strip()
        if raw_text_fallback and str(raw_text_fallback).strip()
        else None,
    )

    combined_context: list[str] = []
    blocked_ssrf_urls: list[str] = []

    for clean_url in validated.urls:
        if not _is_safe_public_url(clean_url):
            blocked_ssrf_urls.append(clean_url)
            emit_audit_log(
                event_type="SSRF_URL_BLOCKED",
                severity="WARNING",
                message=f"Blocked unsafe or private URL in ingest_customer_sources: {clean_url}",
                blocked_url=clean_url,
            )
            combined_context.append(
                f"--- URL Blocked ({clean_url}): Rejected by SSRF / Private IP Security Gate. "
                "Degrading gracefully to raw text fallback. ---"
            )
            continue
        try:
            headers = {"User-Agent": "EnterpriseAgent/1.0"}
            response = requests.get(
                clean_url, headers=headers, timeout=10, allow_redirects=False
            )
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
        except Exception as e:
            combined_context.append(
                f"--- URL Scraping Error ({clean_url}): {str(e)}. "
                "Fallback to raw text context. ---"
            )

    passage_count = 0
    for idx, doc_text in enumerate(validated.raw_docs, 1):
        passage_count += 1
        combined_context.append(f"--- Discovery Document {idx} ---\n{doc_text}")

    if validated.raw_text_fallback:
        passage_count += 1
        combined_context.append(
            f"--- Raw User-Pasted Fallback Context ---\n{validated.raw_text_fallback}"
        )

    raw_unified_text = (
        "\n\n".join(combined_context)
        if combined_context
        else "--- No external URLs or discovery documents provided. Provide raw text context. ---"
    )
    scrubbed_unified_text, pii_redactions = scrub_pii_with_count(raw_unified_text)
    if pii_redactions > 0:
        emit_audit_log(
            event_type="PII_REDACTED",
            severity="INFO",
            message=f"Scrubbed {pii_redactions} PII/secret occurrence(s) from ingested customer discovery sources.",
            pipeline_stage="discovery_ingestion",
            redactions_applied=pii_redactions,
        )
    output_obj = IngestSourcesOutput(
        url_count=len(validated.urls),
        blocked_ssrf_urls=blocked_ssrf_urls,
        passage_count=passage_count,
        pii_redactions_applied=pii_redactions,
        combined_context=scrubbed_unified_text,
    )
    return (
        output_obj.model_dump()
        if hasattr(output_obj, "model_dump")
        else output_obj.__dict__
    )


from .config import build_flash_model, build_gemini_model
from .telemetry import (
    emit_audit_log,
    scrub_pii_with_count,
    start_rpd_span,
    strategic_model_router_callback,
    telemetry_after_model_callback,
    telemetry_after_tool_callback,
    telemetry_before_tool_callback,
    telemetry_on_model_error_callback,
)

discovery_agent = Agent(
    name="discovery_industry_classifier",
    model=build_flash_model(),
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
    output_key="customer_context",
    before_model_callback=[strategic_model_router_callback],
    after_model_callback=[telemetry_after_model_callback],
    before_tool_callback=[telemetry_before_tool_callback],
    after_tool_callback=[telemetry_after_tool_callback],
    on_model_error_callback=[telemetry_on_model_error_callback],
    tools=[ingest_customer_sources],
)
