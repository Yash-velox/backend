from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("app.services.shopify_graphql")


class ShopifyGraphQLError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


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

PRODUCT_PUBLISH_SNAPSHOT_QUERY = """
query ProductPublishSnapshot($id: ID!, $mediaCursor: String) {
  product(id: $id) {
    id
    updatedAt
    featuredMedia {
      ... on MediaImage {
        id
      }
    }
    media(first: 50, after: $mediaCursor) {
      pageInfo {
        hasNextPage
        endCursor
      }
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
    variants(first: 100) {
      nodes {
        id
        title
        media(first: 1) {
          nodes {
            ... on MediaImage {
              id
            }
          }
        }
        image {
          id
          url
        }
      }
    }
  }
}
"""

STAGED_UPLOADS_CREATE = """
mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters {
        name
        value
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

FILE_CREATE = """
mutation FileCreate($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files {
      id
      fileStatus
      alt
      ... on MediaImage {
        id
        image {
          url
          width
          height
        }
      }
    }
    userErrors {
      field
      message
      code
    }
  }
}
"""

FILES_STATUS_QUERY = """
query FilesStatus($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on MediaImage {
      id
      fileStatus
      alt
      image {
        url
        width
        height
      }
    }
    ... on GenericFile {
      id
      fileStatus
      alt
    }
  }
}
"""

FILE_UPDATE = """
mutation FileUpdate($files: [FileUpdateInput!]!) {
  fileUpdate(files: $files) {
    files {
      id
      fileStatus
      alt
      ... on MediaImage {
        id
        image {
          url
        }
      }
    }
    userErrors {
      field
      message
      code
    }
  }
}
"""

PRODUCT_VARIANTS_BULK_UPDATE = """
mutation ProductVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants {
      id
    }
    userErrors {
      field
      message
    }
  }
}
"""

PRODUCT_REORDER_MEDIA = """
mutation ProductReorderMedia($id: ID!, $moves: [MoveInput!]!) {
  productReorderMedia(id: $id, moves: $moves) {
    job {
      id
      done
    }
    mediaUserErrors {
      field
      message
      code
    }
  }
}
"""

JOB_STATUS_QUERY = """
query JobStatus($id: ID!) {
  job(id: $id) {
    id
    done
  }
}
"""


class ShopifyGraphQLClient:
    """Admin GraphQL client for catalog reads and product media publishing."""

    def __init__(
        self,
        *,
        shop_domain: str,
        access_token: str,
        api_version: str | None = None,
        refresh_access_token: Callable[[], str] | None = None,
    ) -> None:
        domain = shop_domain.replace("https://", "").replace("http://", "").rstrip("/")
        version = api_version or settings.shopify_api_version
        self.shop_domain = domain
        self._url = f"https://{domain}/admin/api/{version}/graphql.json"
        self._refresh_access_token = refresh_access_token
        self._unauthorized_retried = False
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
            # Scenario 1: auth failed → refresh shops token once, then retry the same call.
            if (
                exc.status_code == 401
                and self._refresh_access_token is not None
                and not self._unauthorized_retried
            ):
                self._unauthorized_retried = True
                logger.warning(
                    "Shopify GraphQL 401 - refreshing shops token and retrying | shop=%s",
                    self.shop_domain,
                )
                try:
                    new_token = self._refresh_access_token()
                except Exception as refresh_exc:
                    logger.error(
                        "Shopify token refresh after 401 failed | shop=%s error=%s",
                        self.shop_domain,
                        refresh_exc,
                    )
                    raise exc from refresh_exc
                if not new_token:
                    raise
                self._headers["X-Shopify-Access-Token"] = new_token
                return self._execute_once(payload)

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
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise ShopifyGraphQLError(
                f"Shopify GraphQL error HTTP {response.status_code}",
                retryable=False,
                status_code=response.status_code,
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

    def get_product_media_snapshot(self, product_gid: str) -> dict[str, Any] | None:
        """Fetch full product media + variant media associations with media pagination."""
        if not product_gid:
            return None
        media_nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        product_meta: dict[str, Any] | None = None
        while True:
            data = self.execute(
                PRODUCT_PUBLISH_SNAPSHOT_QUERY,
                {"id": product_gid, "mediaCursor": cursor},
            )
            product = data.get("product")
            if not product:
                return None
            if product_meta is None:
                product_meta = {
                    "id": product.get("id"),
                    "updatedAt": product.get("updatedAt"),
                    "featuredMedia": product.get("featuredMedia"),
                    "variants": product.get("variants"),
                }
            block = product.get("media") or {}
            media_nodes.extend([n for n in (block.get("nodes") or []) if n])
            page_info = block.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
        assert product_meta is not None
        return {
            "id": product_meta["id"],
            "updatedAt": product_meta["updatedAt"],
            "featuredMedia": product_meta["featuredMedia"],
            "media": {"nodes": media_nodes},
            "variants": product_meta["variants"],
        }

    def create_staged_image_uploads(self, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        data = self.execute(STAGED_UPLOADS_CREATE, {"input": inputs})
        payload = data.get("stagedUploadsCreate") or {}
        errors = payload.get("userErrors") or []
        if errors:
            msg = "; ".join(str(e.get("message", e)) for e in errors)
            raise ShopifyGraphQLError(f"stagedUploadsCreate failed: {msg}", retryable=False)
        return [t for t in (payload.get("stagedTargets") or []) if t]

    def create_shopify_files(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        data = self.execute(FILE_CREATE, {"files": files})
        payload = data.get("fileCreate") or {}
        errors = payload.get("userErrors") or []
        if errors:
            msg = "; ".join(str(e.get("message", e)) for e in errors)
            raise ShopifyGraphQLError(f"fileCreate failed: {msg}", retryable=False)
        return [f for f in (payload.get("files") or []) if f]

    def get_file_statuses(self, file_gids: list[str]) -> list[dict[str, Any]]:
        if not file_gids:
            return []
        data = self.execute(FILES_STATUS_QUERY, {"ids": file_gids})
        return [n for n in (data.get("nodes") or []) if n]

    def update_files(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        data = self.execute(FILE_UPDATE, {"files": files})
        payload = data.get("fileUpdate") or {}
        errors = payload.get("userErrors") or []
        if errors:
            msg = "; ".join(str(e.get("message", e)) for e in errors)
            raise ShopifyGraphQLError(f"fileUpdate failed: {msg}", retryable=False)
        return [f for f in (payload.get("files") or []) if f]

    def add_file_product_references(self, *, file_gids: list[str], product_gid: str) -> list[dict[str, Any]]:
        inputs = [{"id": gid, "referencesToAdd": [product_gid]} for gid in file_gids]
        return self.update_files(inputs)

    def remove_file_product_references(self, *, file_gids: list[str], product_gid: str) -> list[dict[str, Any]]:
        inputs = [{"id": gid, "referencesToRemove": [product_gid]} for gid in file_gids]
        return self.update_files(inputs)

    def update_file_alt_text(self, *, file_gid: str, alt: str | None) -> dict[str, Any] | None:
        results = self.update_files([{"id": file_gid, "alt": alt or ""}])
        return results[0] if results else None

    def associate_media_to_variants(
        self,
        *,
        product_gid: str,
        variants: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        data = self.execute(
            PRODUCT_VARIANTS_BULK_UPDATE,
            {"productId": product_gid, "variants": variants},
        )
        payload = data.get("productVariantsBulkUpdate") or {}
        errors = payload.get("userErrors") or []
        if errors:
            msg = "; ".join(str(e.get("message", e)) for e in errors)
            raise ShopifyGraphQLError(f"productVariantsBulkUpdate failed: {msg}", retryable=False)
        return [v for v in (payload.get("productVariants") or []) if v]

    def reorder_product_media(self, *, product_gid: str, moves: list[dict[str, Any]]) -> dict[str, Any]:
        data = self.execute(PRODUCT_REORDER_MEDIA, {"id": product_gid, "moves": moves})
        payload = data.get("productReorderMedia") or {}
        errors = payload.get("mediaUserErrors") or []
        if errors:
            msg = "; ".join(str(e.get("message", e)) for e in errors)
            raise ShopifyGraphQLError(f"productReorderMedia failed: {msg}", retryable=False)
        job = payload.get("job") or {}
        return {"id": job.get("id"), "done": bool(job.get("done"))}

    def get_job_status(self, job_gid: str) -> dict[str, Any]:
        data = self.execute(JOB_STATUS_QUERY, {"id": job_gid})
        job = data.get("job") or {}
        return {"id": job.get("id"), "done": bool(job.get("done"))}
