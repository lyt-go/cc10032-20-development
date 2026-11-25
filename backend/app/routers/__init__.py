"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import backfill, resource

# backfill 含更具体的 /api/backfill-jobs/... 路径，必须先注册，
# 否则会被 resource 的 /api/{module} 动态路径抢先匹配。
ROUTERS = [backfill, resource]
