from __future__ import annotations

import logging
import time
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
            updatedAt
            mimeType
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

PRODUCTS_PAGE_QUERY = """
query ProductsPage($first: Int!, $cursor: String) {
  products(first: $first, after: $cursor) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      title
      descriptionHtml
      handle
      status
      productType
      vendor
      tags
      updatedAt
      featuredMedia {
        ... on MediaImage {
          id
        }
      }
      media(first: 50) {
        nodes {
          id
          mediaContentType
          alt
          ... on MediaImage {
            id
            alt
            updatedAt
            mimeType
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
      variants(first: 50) {
        nodes {
          id
          title
          sku
          updatedAt
        }
      }
    }
  }
}
"""

PRODUCT_BY_GID_QUERY = """
query ProductByGid($id: ID!) {
  product(id: $id) {
    id
    title
    descriptionHtml
    handle
    status
    productType
    vendor
    tags
    updatedAt
    featuredMedia {
      ... on MediaImage {
        id
      }
    }
    media(first: 50) {
      nodes {
        id
        mediaContentType
        alt
        ... on MediaImage {
          id
          alt
          updatedAt
          mimeType
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
    variants(first: 50) {
      nodes {
        id
        title
        sku
        updatedAt
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
        return self._execute_with_retry(query, variables, allow_retry=True)

    def _execute_with_retry(
        self,
        query: str,
        variables: dict[str, Any] | None,
        *,
        allow_retry: bool,
    ) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        try:
            return self._execute_once(payload)
        except ShopifyGraphQLError as exc:
            if allow_retry and exc.retryable:
                logger.warning(
                    "Shopify GraphQL retry | shop=%s error=%s",
                    self.shop_domain,
                    exc,
                )
                time.sleep(0.5)
                return self._execute_with_retry(query, variables, allow_retry=False)
            raise

    def _execute_once(self, payload: dict[str, Any]) -> dict[str, Any]:
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
            retryable = any(
                token in message.lower()
                for token in ("throttled", "timeout", "internal", "temporarily")
            )
            raise ShopifyGraphQLError(f"Shopify GraphQL errors: {message}", retryable=retryable)
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
        search_query = q if ":" in q else f"title:*{q}*"
        data = self.execute(PRODUCT_SEARCH_QUERY, {"query": search_query, "first": min(max(first, 1), 50)})
        nodes = ((data.get("products") or {}).get("nodes")) or []
        return [n for n in nodes if n]

    def fetch_products_page(
        self,
        *,
        cursor: str | None = None,
        first: int = 25,
    ) -> dict[str, Any]:
        page_size = min(max(first, 1), 50)
        data = self.execute(PRODUCTS_PAGE_QUERY, {"first": page_size, "cursor": cursor})
        products_block = data.get("products") or {}
        nodes = [n for n in (products_block.get("nodes") or []) if n]
        page_info = products_block.get("pageInfo") or {}
        return {
            "products": nodes,
            "pageInfo": {
                "hasNextPage": bool(page_info.get("hasNextPage")),
                "endCursor": page_info.get("endCursor"),
            },
        }

    def fetch_product_by_gid(self, gid: str) -> dict[str, Any] | None:
        if not gid:
            return None
        data = self.execute(PRODUCT_BY_GID_QUERY, {"id": gid})
        return data.get("product")
