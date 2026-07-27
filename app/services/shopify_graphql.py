from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("app.services.shopify_graphql")


class ShopifyGraphQLError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


PRODUCT_MEDIA_QUERY = """
query ProductMedia($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Product {
      id
      title
      media(first: 50) {
        nodes {
          id
          mediaContentType
          ... on MediaImage {
            id
            alt
            image {
              url
              width
              height
            }
            originalSource {
              fileSize
              url
            }
          }
        }
      }
    }
  }
}
"""

PRODUCT_SEARCH_QUERY = """
query ProductSearch($query: String!, $first: Int!) {
  products(first: $first, query: $query) {
    nodes {
      id
      title
      handle
      status
      featuredImage {
        url
        altText
      }
    }
  }
}
"""


class ShopifyGraphQLClient:
    """Minimal Admin GraphQL client — read-only for this phase."""

    def __init__(self, *, shop_domain: str, access_token: str, api_version: str | None = None) -> None:
        domain = shop_domain.replace("https://", "").replace("http://", "").rstrip("/")
        version = api_version or settings.shopify_api_version
        self.shop_domain = domain
        self._url = f"https://{domain}/admin/api/{version}/graphql.json"
        self._headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        try:
            response = httpx.post(self._url, headers=self._headers, json=payload, timeout=30.0)
        except httpx.TimeoutException as exc:
            raise ShopifyGraphQLError("Shopify GraphQL request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ShopifyGraphQLError(f"Shopify GraphQL network error: {exc}", retryable=True) from exc

        if response.status_code in {429, 500, 502, 503, 504}:
            raise ShopifyGraphQLError(
                f"Shopify GraphQL temporary error HTTP {response.status_code}",
                retryable=True,
            )
        if response.status_code >= 400:
            raise ShopifyGraphQLError(
                f"Shopify GraphQL error HTTP {response.status_code}",
                retryable=False,
            )

        body = response.json()
        if body.get("errors"):
            message = "; ".join(str(e.get("message", e)) for e in body["errors"])
            raise ShopifyGraphQLError(f"Shopify GraphQL errors: {message}", retryable=False)
        return body.get("data") or {}

    def fetch_products_media(self, product_gids: list[str]) -> list[dict[str, Any]]:
        if not product_gids:
            return []
        data = self.execute(PRODUCT_MEDIA_QUERY, {"ids": product_gids})
        nodes = data.get("nodes") or []
        return [n for n in nodes if n]

    def search_products(self, query: str, *, first: int = 20) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        # Prefer title match; Shopify also accepts free-text product search.
        search_query = q if ":" in q else f"title:*{q}*"
        data = self.execute(PRODUCT_SEARCH_QUERY, {"query": search_query, "first": min(max(first, 1), 50)})
        nodes = ((data.get("products") or {}).get("nodes")) or []
        return [n for n in nodes if n]
