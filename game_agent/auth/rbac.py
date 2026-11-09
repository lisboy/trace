"""RBAC：4 种人类角色的权限矩阵（设计 §3.2 / §3.3）。

权限在后端强制（每个路由显式声明所需权限）；前端只做体验，不做安全闸门。
"""
from __future__ import annotations

# 权限常量
PERM_VIEW = "view"                      # 查看会话/事件流/产物
PERM_DOWNLOAD = "download"              # 下载产物
PERM_RUN_CREATE = "run:create"          # 发起 run
PERM_RUN_CANCEL_SELF = "run:cancel:self"
PERM_RUN_CANCEL_OTHERS = "run:cancel:others"
PERM_COMMENT = "comment"
PERM_APPROVAL = "approval:decide"       # 审批合并等
PERM_AGENT_CONFIG = "agent:config"      # 改智能体 prompt / 绑模型
PERM_PROVIDER_CONFIG = "provider:config"
PERM_PROJECT_CREATE = "project:create"
PERM_PROJECT_ARCHIVE = "project:archive"
PERM_BUDGET = "budget:config"
PERM_MEMBER = "member:manage"
PERM_TEAM_DELETE = "team:delete"
PERM_USAGE_VIEW = "usage:view"
PERM_RUN_READ = "run:read"

# 每个角色拥有的权限集合
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {
        PERM_VIEW, PERM_DOWNLOAD, PERM_RUN_CREATE, PERM_RUN_CANCEL_SELF,
        PERM_RUN_CANCEL_OTHERS, PERM_COMMENT, PERM_APPROVAL, PERM_AGENT_CONFIG,
        PERM_PROVIDER_CONFIG, PERM_PROJECT_CREATE, PERM_PROJECT_ARCHIVE,
        PERM_BUDGET, PERM_MEMBER, PERM_TEAM_DELETE, PERM_USAGE_VIEW, PERM_RUN_READ,
    },
    "admin": {
        PERM_VIEW, PERM_DOWNLOAD, PERM_RUN_CREATE, PERM_RUN_CANCEL_SELF,
        PERM_RUN_CANCEL_OTHERS, PERM_COMMENT, PERM_APPROVAL, PERM_AGENT_CONFIG,
        PERM_PROVIDER_CONFIG, PERM_PROJECT_CREATE, PERM_PROJECT_ARCHIVE,
        PERM_BUDGET, PERM_MEMBER, PERM_USAGE_VIEW, PERM_RUN_READ,
    },
    "developer": {
        PERM_VIEW, PERM_DOWNLOAD, PERM_RUN_CREATE, PERM_RUN_CANCEL_SELF,
        PERM_COMMENT, PERM_RUN_READ,
    },
    "viewer": {
        PERM_VIEW, PERM_DOWNLOAD, PERM_COMMENT, PERM_RUN_READ,
    },
}


def check_permission(role: str, perm: str) -> bool:
    return perm in ROLE_PERMISSIONS.get(role, set())
