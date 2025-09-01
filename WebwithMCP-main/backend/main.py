# main.py
"""
FastAPI 后端主文件
提供WebSocket聊天接口和REST API
"""

import json
import asyncio
import uuid
from typing import List, Dict, Any
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import os
from dotenv import load_dotenv, find_dotenv
import uvicorn


from mcp_agent import WebMCPAgent
from sqldatabase import ChatSqlDatabase

# 全局变量
mcp_agent = None
chat_db = None  # SQLite数据库实例
active_connections: List[WebSocket] = []

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global mcp_agent, chat_db
    
    # 启动时初始化
    print("🚀 启动 MCP Web 智能助手...")
    
    # 初始化数据库
    chat_db = ChatSqlDatabase()
    db_success = await chat_db.initialize()
    if not db_success:
        print("❌ 数据库初始化失败")
        raise Exception("数据库初始化失败")

    # 初始化MCP智能体
    mcp_agent = WebMCPAgent()
    mcp_success = await mcp_agent.initialize()
    
    if not mcp_success:
        print("❌ MCP智能体初始化失败")
        raise Exception("MCP智能体初始化失败")
    
    print("✅ MCP Web 智能助手启动成功")
    
    yield
    
    # 关闭时清理资源
    if mcp_agent:
        await mcp_agent.close()
    if chat_db:
        await chat_db.close()
    print("👋 MCP Web 智能助手已关闭")

# 创建FastAPI应用
# 预加载 .env（不覆盖系统变量）
try:
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

app = FastAPI(
    title="MCP Web智能助手",
    description="基于MCP的智能助手Web版",
    version="1.0.0",
    lifespan=lifespan
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# ─────────── WebSocket 接口 ───────────

class ConnectionManager:
    """WebSocket连接管理器"""
    
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.connection_sessions: Dict[WebSocket, str] = {}  # 连接到会话ID的映射
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        # 如果前端用户选择继承历史对话 则通过query_params对象中获取已有的session_id
        # 否则为每个连接生成唯一的会话ID
        # 调试阶段 写死user_id
        # user_id = websocket.query_params.get("user_id")
        user_id = "kuizi"
        if user_id is None:
            raise ValueError("❌ 缺少必要参数: user_id")
        if "session_id" in websocket.query_params:
            session_id = websocket.query_params["session_id"]
        else:
            session_id = str(uuid.uuid4())

        self.active_connections.append(websocket)
        self.connection_sessions[websocket] = session_id
        print(f"📱 新连接建立，会话ID: {session_id}，当前连接数: {len(self.active_connections)}")
        
        # 向前端发送会话ID
        await self.send_personal_message({
            "type": "session_info",
            "session_id": session_id
        }, websocket)
        
        return {"session_id":session_id,
                "user_id":user_id}
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if websocket in self.connection_sessions:
            session_id = self.connection_sessions[websocket]
            del self.connection_sessions[websocket]
            print(f"📱 连接断开，会话ID: {session_id}，当前连接数: {len(self.active_connections)}")
    
    def get_session_id(self, websocket: WebSocket) -> str:
        """获取WebSocket连接对应的会话ID"""
        return self.connection_sessions.get(websocket, "default")
    
    async def send_personal_message(self, message: dict, websocket: WebSocket):
        try:
            await websocket.send_text(json.dumps(message, ensure_ascii=False))
        except Exception as e:
            print(f"❌ 发送消息失败: {e}")

manager = ConnectionManager()

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket聊天接口"""
    dict = await manager.connect(websocket)
    session_id = dict.get("session_id")
    user_id = "kuizi"
    try:
        while True:
            # 接收客户端消息
            data = await websocket.receive_text()
            
            try:
                message = json.loads(data)
                
                if message.get("type") == "user_msg":
                    user_input = message.get("content", "").strip()
                    
                    if not user_input:
                        await manager.send_personal_message({
                            "type": "error",
                            "content": "用户输入不能为空"
                        }, websocket)
                        continue
                    
                    print(f"📨 收到用户消息: {user_input[:50]}...")
                    
                    # 确认收到用户消息
                    await manager.send_personal_message({
                        "type": "user_msg_received",
                        "content": user_input
                    }, websocket)
                    
                    # 收集对话数据
                    conversation_data = {
                        "user_input": user_input,
                        "mcp_tools_called": [],
                        "mcp_results": [],
                        "ai_response_parts": []
                    }
                    
                    # 获取当前连接的聊天历史
                    history = await chat_db.get_chat_history(session_id=session_id, limit=10) # 限制最近10条

                    # 流式处理并推送AI响应
                    try:
                        async for response_chunk in mcp_agent.chat_stream(user_input, history=history):
                            # 转发给客户端 客户端根据type进行渲染
                            await manager.send_personal_message(response_chunk, websocket)
                            
                            # 收集不同类型的响应数据
                            chunk_type = response_chunk.get("type")
                            
                            if chunk_type == "tool_start":
                                # 记录工具调用开始
                                tool_call = {
                                    "tool_id": response_chunk.get("tool_id"),
                                    "tool_name": response_chunk.get("tool_name"),
                                    "tool_args": response_chunk.get("tool_args"),
                                    "progress": response_chunk.get("progress")
                                }
                                conversation_data["mcp_tools_called"].append(tool_call)
                            
                            elif chunk_type == "tool_end":
                                # 记录工具执行结果
                                tool_result = {
                                    "tool_id": response_chunk.get("tool_id"),
                                    "tool_name": response_chunk.get("tool_name"),
                                    "result": response_chunk.get("result"),
                                    "success": True
                                }
                                conversation_data["mcp_results"].append(tool_result)
                            
                            elif chunk_type == "tool_error":
                                # 记录工具执行错误
                                tool_error = {
                                    "tool_id": response_chunk.get("tool_id"),
                                    "error": response_chunk.get("error"),
                                    "success": False
                                }
                                conversation_data["mcp_results"].append(tool_error)
                            
                            elif chunk_type == "ai_response_chunk":
                                # 收集AI回复内容片段
                                conversation_data["ai_response_parts"].append(
                                    response_chunk.get("content", "")
                                )
                            
                            elif chunk_type == "ai_thinking_chunk":
                                # 收集AI思考内容片段到回复中
                                conversation_data["ai_response_parts"].append(
                                    response_chunk.get("content", "")
                                )
                            
                            elif chunk_type == "error":
                                # 记录错误信息
                                print(f"❌ MCP处理错误: {response_chunk.get('content')}")
                                # 即使出错也要保存对话记录
                                break
                        
                        # 组装完整的AI回复
                        ai_response = "".join(conversation_data["ai_response_parts"])
                        
                        # 如果没有AI回复但有错误，添加错误信息
                        if not ai_response and conversation_data["mcp_results"]:
                            error_results = [r for r in conversation_data["mcp_results"] if not r.get("success", True)]
                            if error_results:
                                ai_response = f"处理过程中遇到错误：\n" + "\n".join([r.get("error", "未知错误") for r in error_results])
                        
                        print(f"💾 准备保存对话记录，AI回复长度: {len(ai_response)}")
                        
                    except Exception as e:
                        print(f"❌ MCP流式处理异常: {e}")
                        import traceback
                        traceback.print_exc()
                        
                        # 即使异常也要保存对话记录
                        ai_response = f"处理请求时出错: {str(e)}"
                        conversation_data["ai_response_parts"] = [ai_response]
                    
                    # 保存完整对话到数据库
                    if chat_db:
                        try:
                            success = await chat_db.save_conversation(
                                user_input=conversation_data["user_input"],
                                mcp_tools_called=conversation_data["mcp_tools_called"],
                                mcp_results=conversation_data["mcp_results"],
                                ai_response=ai_response,
                                session_id=session_id,
                                user_id = user_id
                            )
                            if success:
                                print(f"✅ 对话记录保存成功")
                            else:
                                print(f"❌ 对话记录保存失败")
                        except Exception as e:
                            print(f"❌ 保存对话记录异常: {e}")
                
                elif message.get("type") == "ping":
                    # 心跳响应
                    await manager.send_personal_message({
                        "type": "pong",
                        "timestamp": datetime.now().isoformat()
                    }, websocket)
                
                else:
                    await manager.send_personal_message({
                        "type": "error",
                        "content": f"未知消息类型: {message.get('type')}"
                    }, websocket)
                    
            except json.JSONDecodeError:
                await manager.send_personal_message({
                    "type": "error",
                    "content": "消息格式错误，请发送有效的JSON"
                }, websocket)
            except Exception as e:
                print(f"❌ WebSocket消息处理异常: {e}")
                import traceback
                traceback.print_exc()
                await manager.send_personal_message({
                    "type": "error",
                    "content": f"处理消息时出错: {str(e)}"
                }, websocket)
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"❌ WebSocket错误: {e}")
        manager.disconnect(websocket)


# --------------------LLM http调用接口----------------------
from pydantic import BaseModel, Field
from fastapi import Body
from fastapi.responses import StreamingResponse

class ChatRequest(BaseModel):
    user_id: str = Field(..., description="必填，调用方用户ID")
    content: str = Field(..., description="自然语言输入")
    session_id: str | None = Field(None, description="可选；传入则复用会话，不传服务器将新建")

class ChatResult(BaseModel):
    success: bool
    session_id: str
    user_id: str
    answer: str
    used_tools: list[dict] = []
    errors: list[str] = []
    timestamp: str


async def _run_chat_once(user_id: str, content: str, session_id: str | None):
    """
    复用 ws/chat 的核心流程：生成/复用 session_id → 读历史 → chat_stream → 汇总 → 落库
    """
    if not mcp_agent:
        raise HTTPException(status_code=503, detail="MCP智能体未初始化")
    if not chat_db:
        raise HTTPException(status_code=503, detail="数据库未初始化")

    # session_id：复用或生成
    sid = session_id or str(uuid.uuid4())
    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="用户输入不能为空")
    content = content.strip()

    # 收集轨迹
    conversation_data = {
        "user_input": content,
        "mcp_tools_called": [],
        "mcp_results": [],
        "ai_response_parts": [],
    }

    # 读取最近历史（与 ws 对齐：取最近10条即可）
    history = await chat_db.get_chat_history(session_id=sid, limit=10)

    # 调用 LLM 流（与 ws/chat 逻辑一致）
    try:
        # 流式返回结果 前端需要模拟同样的流式返回接受
        async for response_chunk in mcp_agent.chat_stream(content, history=history):
            # 收集不同类型的响应数据

            #调用的原生信息收集 并且落库保存
            chunk_type = response_chunk.get("type")
            if chunk_type == "tool_start":
                conversation_data["mcp_tools_called"].append({
                    "tool_id": response_chunk.get("tool_id"),
                    "tool_name": response_chunk.get("tool_name"),
                    "tool_args": response_chunk.get("tool_args"),
                    "progress": response_chunk.get("progress"),
                })
            elif chunk_type == "tool_end":
                conversation_data["mcp_results"].append({
                    "tool_id": response_chunk.get("tool_id"),
                    "tool_name": response_chunk.get("tool_name"),
                    "result": response_chunk.get("result"),
                    "success": True,
                })
            elif chunk_type == "tool_error":
                conversation_data["mcp_results"].append({
                    "tool_id": response_chunk.get("tool_id"),
                    "error": response_chunk.get("error"),
                    "success": False,
                })
            elif chunk_type in ("ai_response_chunk", "ai_thinking_chunk"):
                conversation_data["ai_response_parts"].append(response_chunk.get("content", ""))
            elif chunk_type == "error":
                # 有错误也继续汇总，最终答复里会体现
                conversation_data["ai_response_parts"].append(
                    f"\n[处理错误] {response_chunk.get('content','未知错误')}"
                )
    except Exception as e:
        # 兜底：流式处理异常也返回一个错误答复并落库
        conversation_data["ai_response_parts"].append(f"[异常] {str(e)}")

    # 组装完整答复
    ai_response = "".join(conversation_data["ai_response_parts"]) or ""
    # 若无答复但存在错误结果，拼上报错
    if not ai_response and conversation_data["mcp_results"]:
        error_results = [r for r in conversation_data["mcp_results"] if not r.get("success", True)]
        if error_results:
            ai_response = "处理过程中遇到错误：\n" + "\n".join([r.get("error", "未知错误") for r in error_results])

    # 落库
    try:
        await chat_db.save_conversation(
            user_input=conversation_data["user_input"],
            mcp_tools_called=conversation_data["mcp_tools_called"],
            mcp_results=conversation_data["mcp_results"],
            ai_response=ai_response,
            session_id=sid,
            user_id=user_id,
        )
    except Exception as e:
        # 不阻断主流程
        print(f"❌ 保存对话记录异常: {e}")

    # 汇总返回
    used_tools = conversation_data["mcp_tools_called"] + conversation_data["mcp_results"]
    errors = [r.get("error") for r in conversation_data["mcp_results"] if not r.get("success", True)]
    return ChatResult(
        success=True,
        session_id=sid,
        user_id=user_id,
        answer=ai_response,
        used_tools=used_tools,
        errors=[e for e in errors if e],
        timestamp=datetime.now().isoformat()
    )

#一次性返回整个与LLM的对话结果内容
@app.post("/api/chat", response_model=ChatResult)
async def chat_http(req: ChatRequest = Body(...)):
    """
    普通 HTTP 调用：接收自然语言，调用 LLM，返回完整回答
    - 必填: user_id, content
    - 可选: session_id (不传则自动生成并复用到数据库会话里)
    """
    return await _run_chat_once(
        user_id=req.user_id,
        content=req.content,
        session_id=req.session_id,
    )

#http的流式返回给springboot作为中介转发  实现类似聊天机器人的效果
@app.post("/stream/chat")
async def chat_http_stream(req: ChatRequest = Body(...)):
    """
    返回 Content-Type: application/x-ndjson
    每一行是一条事件JSON，与 WebSocket 版本的 'type' 对齐：
    - status / ai_thinking_* / tool_* / ai_response_* / error / done / session_info
    """
    if not mcp_agent:
        raise HTTPException(status_code=503, detail="MCP智能体未初始化")
    if not chat_db:
        raise HTTPException(status_code=503, detail="数据库未初始化")

    user_id = (req.user_id or "").strip()
    content = (req.content or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="缺少 user_id")
    if not content:
        raise HTTPException(status_code=400, detail="用户输入不能为空")

    # 复用或生成会话
    session_id = (req.session_id or str(uuid.uuid4())).strip()

    # 取最近历史（与你的 WS 版本一致）
    history = await chat_db.get_chat_history(session_id=session_id, limit=10)

    async def gen():
        # 先把会话ID发给前端，便于复用
        yield json.dumps({"type": "session_info", "session_id": session_id}, ensure_ascii=False) + "\n"

        # 与 WS 版一致的收集容器
        conversation_data = {
            "user_input": content,
            "mcp_tools_called": [],
            "mcp_results": [],
            "ai_response_parts": []
        }

        try:
            # 逐事件透传（并顺便收集要落库的信息）
            async for response_chunk in mcp_agent.chat_stream(content, history=history):
                t = response_chunk.get("type")

                if t == "tool_start":
                    conversation_data["mcp_tools_called"].append({
                        "tool_id": response_chunk.get("tool_id"),
                        "tool_name": response_chunk.get("tool_name"),
                        "tool_args": response_chunk.get("tool_args"),
                        "progress": response_chunk.get("progress")
                    })
                elif t == "tool_end":
                    conversation_data["mcp_results"].append({
                        "tool_id": response_chunk.get("tool_id"),
                        "tool_name": response_chunk.get("tool_name"),
                        "result": response_chunk.get("result"),
                        "success": True
                    })

                elif t == "tool_error":
                    conversation_data["mcp_results"].append({
                        "tool_id": response_chunk.get("tool_id"),
                        "error": response_chunk.get("error"),
                        "success": False
                    })

                elif t in ("ai_response_chunk", "ai_thinking_chunk"):
                    conversation_data["ai_response_parts"].append(response_chunk.get("content", ""))

                elif t == "error":
                    # 出错同样写一条出去
                    print(f"❌ MCP处理错误: {response_chunk.get('content')}")
                    break
                # 把当前事件按行发给前端
                yield json.dumps(response_chunk, ensure_ascii=False) + "\n"

        except Exception as e:
            # 流式过程中异常，通知前端，但仍尝试落库
            err = {"type": "error", "content": f"流式处理异常: {str(e)}"}
            yield json.dumps(err, ensure_ascii=False) + "\n"

        # —— 结束前落库——
        ai_response = "".join(conversation_data["ai_response_parts"])

        if not ai_response and conversation_data["mcp_results"]:
            error_results = [r for r in conversation_data["mcp_results"] if not r.get("success", True)]
            if error_results:
                ai_response = "处理过程中遇到错误：\n" + "\n".join([r.get("error", "未知错误") for r in error_results])

        print(f"💾 准备保存对话记录，AI回复长度: {len(ai_response)}")

        if chat_db:
            try:
                success = await chat_db.save_conversation(
                    user_input=conversation_data["user_input"],
                    mcp_tools_called=conversation_data["mcp_tools_called"],
                    mcp_results=conversation_data["mcp_results"],
                    ai_response=ai_response,
                    session_id=session_id,
                    user_id=user_id
                )
                if not success:
                    warn = {"type": "warn", "content": "对话记录保存失败"}
                    print(f"❌ 对话记录保存失败")
                    yield json.dumps(warn, ensure_ascii=False) + "\n"
            except Exception as e:
                warn = {"type": "warn", "content": f"保存对话记录异常: {str(e)}"}
                yield json.dumps(warn, ensure_ascii=False) + "\n"

        # 结束标记
        yield json.dumps({"type": "done"}, ensure_ascii=False) + "\n"

    # 重要：NDJSON 媒体类型 + 禁缓存
    headers = {"Cache-Control": "no-cache"}
    return StreamingResponse(gen(), media_type="application/x-ndjson", headers=headers)

# ─────────── REST API 接口 ───────────

@app.get("/")
async def root():
    """根路径重定向到前端"""
    return {"message": "MCP Web智能助手API", "version": "1.0.0"}

@app.get("/api/tools")
async def get_tools():
    """获取可用工具列表"""
    if not mcp_agent:
        raise HTTPException(status_code=503, detail="MCP智能体未初始化")
    
    try:
        tools_info = mcp_agent.get_tools_info()
        return {
            "success": True,
            "data": tools_info
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取工具列表失败: {str(e)}")

@app.get("/api/history")
async def get_history(limit: int = 50, session_id: str = "default", conversation_id: int = None):
    """获取聊天历史"""
    if not chat_db:
        raise HTTPException(status_code=503, detail="数据库未初始化")
    
    try:
        records = await chat_db.get_chat_history(
            session_id=session_id, 
            limit=limit,
            conversation_id=conversation_id
        )
        
        # 获取统计信息
        stats = await chat_db.get_stats()
        
        return {
            "success": True,
            "data": records,
            "total": stats.get("total_records", 0),
            "returned": len(records),
            "session_id": session_id,
            "conversation_id": conversation_id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取历史记录失败: {str(e)}")

@app.delete("/api/history")
async def clear_history(session_id: str = None):
    """清空聊天历史"""
    if not chat_db:
        raise HTTPException(status_code=503, detail="数据库未初始化")
    
    try:
        # 如果没有提供session_id，则清空所有历史（保持向后兼容）
        if session_id:
            success = await chat_db.clear_history(session_id=session_id)
            message = f"会话 {session_id} 的聊天历史已清空"
        else:
            success = await chat_db.clear_history()
            message = "所有聊天历史已清空"
        
        if success:
            return {"success": True, "message": message}
        else:
            raise HTTPException(status_code=500, detail="清空历史记录失败")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清空历史记录失败: {str(e)}")

@app.get("/api/status")
async def get_status():
    """获取系统状态"""
    # 获取数据库统计信息
    db_stats = {}
    if chat_db:
        try:
            db_stats = await chat_db.get_stats()
        except Exception as e:
            print(f"⚠️ 获取数据库统计失败: {e}")
    
    return {
        "success": True,
        "data": {
            "agent_initialized": mcp_agent is not None,
            "database_initialized": chat_db is not None,
            "tools_count": len(mcp_agent.tools) if mcp_agent else 0,
            "active_connections": len(manager.active_connections),
            "chat_records_count": db_stats.get("total_records", 0),
            "chat_sessions_count": db_stats.get("total_sessions", 0),
            "chat_conversations_count": db_stats.get("total_conversations", 0),
            "latest_record": db_stats.get("latest_record"),
            "database_path": db_stats.get("database_path"),
            "timestamp": datetime.now().isoformat()
        }
    }

@app.get("/api/database/stats")
async def get_database_stats():
    """获取数据库详细统计信息"""
    if not chat_db:
        raise HTTPException(status_code=503, detail="数据库未初始化")
    
    try:
        stats = await chat_db.get_stats()
        return {
            "success": True,
            "data": stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取数据库统计失败: {str(e)}")

@app.get("/api/share/{session_id}")
async def get_shared_chat(session_id: str, limit: int = 100):
    """获取分享的聊天记录（只读）"""
    if not chat_db:
        raise HTTPException(status_code=503, detail="数据库未初始化")
    
    try:
        # 获取指定会话的聊天历史
        records = await chat_db.get_chat_history(
            session_id=session_id, 
            limit=limit
        )
        
        if not records:
            raise HTTPException(status_code=404, detail="未找到该会话的聊天记录")
        
        # 获取会话统计信息
        stats = await chat_db.get_stats()
        
        return {
            "success": True,
            "data": records,
            "session_id": session_id,
            "total_records": len(records),
            "shared_at": datetime.now().isoformat(),
            "readonly": True
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取分享聊天记录失败: {str(e)}")

# ─────────── 静态文件服务（可选） ───────────

# 如果要让FastAPI直接服务前端文件，取消下面的注释
# app.mount("/static", StaticFiles(directory="../frontend"), name="static")

if __name__ == "__main__":
    # 开发环境启动
    # 端口可通过环境变量 BACKEND_PORT 覆盖，默认 8003，与前端配置一致

    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    # 明确不走代理的地址
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    try:
        port = int(os.getenv("BACKEND_PORT", "8003"))
    except Exception:
        port = 8003
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info"
    )