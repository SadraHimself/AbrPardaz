from aiogram import Router

from .admin import router as admin_router
from .crypto_payment import router as crypto_payment_router
from .admin_broadcast import router as admin_broadcast_router
from .admin_gcore import router as admin_gcore_router
from .admin_hetzner import router as admin_hetzner_router
from .admin_restore import router as admin_restore_router
from .admin_stats import router as admin_stats_router
from .admin_users import router as admin_users_router
from .auth import router as auth_router
from .billing import router as billing_router
from .servers import router as servers_router
from .snapshots import router as snapshots_router
from .start import router as start_router
from .zarinpal_payment import router as zarinpal_payment_router


def setup_routers(dp):
    """Register all routers on the dispatcher."""
    dp.include_router(start_router)
    dp.include_router(auth_router)
    dp.include_router(servers_router)
    dp.include_router(snapshots_router)
    dp.include_router(billing_router)
    dp.include_router(crypto_payment_router)
    dp.include_router(zarinpal_payment_router)
    # Admin routers — order matters: specific before generic
    dp.include_router(admin_users_router)
    dp.include_router(admin_stats_router)
    dp.include_router(admin_broadcast_router)
    dp.include_router(admin_hetzner_router)
    dp.include_router(admin_gcore_router)
    dp.include_router(admin_restore_router)
    dp.include_router(admin_router)
