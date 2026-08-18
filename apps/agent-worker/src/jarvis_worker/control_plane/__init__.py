"""Python Control Plane — FastAPI Internal API。

只处理短事务（创建任务、查询历史、取消、权限决定），不执行 AgentRun 长任务。
仅监听 127.0.0.1 或明确内部网络。
"""
