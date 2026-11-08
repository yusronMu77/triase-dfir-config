"""Endpoint internal config (`/internal/v1`).

Setiap endpoint wajib token layanan: tambahkan parameter
`caller: ServicePrincipal = Depends(require_service("svc:config"))` dari `astria_common.auth`.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/internal/v1", tags=["config"])
