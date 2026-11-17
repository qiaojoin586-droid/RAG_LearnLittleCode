"""Stable domain errors; no provider input is interpolated into messages."""
from app.core.failed_response import BusinessError


def fail(code: int, reason: str, status: int = 409, **metadata):
    raise BusinessError(code, message={
        40312:'没有模型配置管理权限',40314:'账户不可用',40920:'活动配置已更新',
        40921:'草稿已更新',40922:'幂等键已用于其他请求',40924:'草稿已过期',40925:'草稿与活动发布存在冲突，请选择保留的版本',
        40926:'凭据已撤销',40927:'请先更新草稿基线',40928:'不能移除最后一名管理员',
        40929:'请求重放期限已过',40930:'此功能尚未开放',42222:'模型能力尚未验证',
        42801:'缺少 If-Match 版本',50303:'配置存储或主密钥不可用',
        50304:'配置管理实例被其他进程占用',50305:'模型配置尚未发布',
        40110:'需要重新认证',40111:'重新认证证明无效',
    }.get(code,'配置操作未能完成'),http_status=status,data={'reason':reason,**metadata})
