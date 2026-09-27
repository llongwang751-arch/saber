"""Document routes registered by the application composition root."""

import logging
import os
from typing import List

from fastapi import HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from internal.application.api import current_agent
from internal.document.library import DOCUMENT_SOURCE_UPLOAD, WriteRequest
from internal.document.parser import parse_bytes
from internal.rag.lab import run_rag_lab

from .models import DocsDeleteRequest, RAGLabRequest
from .http_contracts import _jsonable, _normalize_ingest_result

logger = logging.getLogger(__name__)


def register_document_routes(app, agent, inf, cfg):
    upload_allowed_extensions = {".md", ".markdown", ".txt", ".pdf"}

    @app.post("/api/rag/lab/run")
    async def rag_lab_run(req: RAGLabRequest, request: Request):
        """运行隔离的 RAG 教学流水线，不向正式知识库写入任何数据。"""
        try:
            active_agent = current_agent(request)
            return await run_in_threadpool(
                run_rag_lab,
                active_agent,
                req.document,
                req.query,
                req.top_k,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("RAG 实验台运行失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/rag/reindex")
    async def rag_reindex(request: Request):
        """Rebuild idempotent search projections from the primary chunk store."""

        rag = getattr(current_agent(request), "rag", None)
        if rag is None or not hasattr(rag, "rebuild_indexes"):
            raise HTTPException(status_code=503, detail="RAG 不可用")
        try:
            return await run_in_threadpool(rag.rebuild_indexes)
        except Exception as exc:
            logger.error("RAG 索引重建失败: %s", exc)
            raise HTTPException(status_code=500, detail="RAG 索引重建失败")

    @app.post("/api/docs/delete")
    async def docs_delete(req: DocsDeleteRequest, request: Request):
        try:
            rag = getattr(current_agent(request), "rag", None)
            if rag is None or not hasattr(rag, "delete"):
                raise HTTPException(status_code=503, detail="RAG 服务不可用，无法删除文档")
            await run_in_threadpool(rag.delete, req.doc_hash)
            return {"ok": True, "doc_hash": req.doc_hash}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("删除文档失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    def _upload_max_bytes() -> int:
        return max(1024, int(os.getenv("AGI_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024))))

    def _validate_upload_filename(filename: str) -> None:
        suffix = os.path.splitext(str(filename).replace("\\", "/").split("/")[-1])[1].lower()
        if suffix not in upload_allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型 {suffix or '(无扩展名)'}，仅允许 md/markdown/txt/pdf",
            )

    @app.post("/api/upload")
    async def upload(request: Request):
        try:
            active_agent = current_agent(request)
            content_type = request.headers.get("content-type", "")
            filename = "upload.txt"
            upload_content_type = "text/plain"
            if "application/json" in content_type:
                payload = await request.json()
                raw_text = str((payload or {}).get("content", ""))
                if len(raw_text.encode("utf-8")) > _upload_max_bytes():
                    raise HTTPException(status_code=413, detail="文档内容超过大小限制")
                parsed = parse_bytes(filename, upload_content_type, raw_text.encode("utf-8"))
            else:
                form = await request.form()
                file = form.get("file")
                if file is None:
                    raise HTTPException(status_code=400, detail="缺少 file 或 content")
                filename = getattr(file, "filename", None) or filename
                _validate_upload_filename(filename)
                upload_content_type = getattr(file, "content_type", None) or upload_content_type
                # 分块读取并累计计量：既避免整包读入内存，也让 chunked 编码
                # （无 Content-Length，绕过全局 body 中间件）同样受大小上限约束。
                parts: List[bytes] = []
                total = 0
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _upload_max_bytes():
                        raise HTTPException(status_code=413, detail="文件超过大小限制")
                    parts.append(chunk)
                parsed = parse_bytes(filename, upload_content_type, b"".join(parts))

            text = parsed.content
            if parsed.needs_ocr:
                return {
                    "filename": parsed.filename,
                    "content_type": parsed.content_type,
                    "parser": parsed.parser,
                    "pages": parsed.pages,
                    "text_chars": parsed.text_chars,
                    "needs_ocr": True,
                    "chunk_count": 0,
                    "parent_count": 0,
                    "indexed_count": 0,
                    "doc_hash": "",
                    "chunks": None,
                    "message": "PDF 文本抽取结果过少，可能是扫描件，需要 OCR 后再入库",
                }
            if not text.strip():
                return {"chunk_count": 0, "doc_hash": "", "success": False, "message": "文件内容为空"}
            doc_result = None
            ingest_result = None
            if hasattr(active_agent, "write_document"):
                write_request = WriteRequest(
                    title=filename,
                    doc_type="upload",
                    source=DOCUMENT_SOURCE_UPLOAD,
                    created_by="user",
                    content_md=text,
                    metadata={
                        "filename": parsed.filename,
                        "content_type": parsed.content_type,
                        "parser": parsed.parser,
                        "pages": parsed.pages,
                        "text_chars": parsed.text_chars,
                    },
                )
                # Document parsing, embedding and index writes are synchronous today.
                # Keep them off the asyncio event loop so a slow embedding provider
                # does not freeze health checks, chat streams and other users.
                doc_result = await run_in_threadpool(
                    active_agent.write_document,
                    write_request,
                    True,
                )
                ingest_result = (doc_result or {}).get("ingest")
            else:
                ingest_result = await run_in_threadpool(active_agent.rag_ingest, text)
            doc_json = _jsonable((doc_result or {}).get("document")) if isinstance(doc_result, dict) else None
            ver_json = _jsonable((doc_result or {}).get("version")) if isinstance(doc_result, dict) else None
            ingest = _normalize_ingest_result(
                ingest_result,
                text,
                document_id=(doc_json or {}).get("id", "") if isinstance(doc_json, dict) else "",
                version_id=(ver_json or {}).get("id", "") if isinstance(ver_json, dict) else "",
                section="upload",
            )
            return {
                "filename": parsed.filename,
                "content_type": parsed.content_type,
                "parser": parsed.parser,
                "pages": parsed.pages,
                "text_chars": parsed.text_chars,
                "needs_ocr": parsed.needs_ocr,
                "chunk_count": ingest.get("chunk_count", 0),
                "parent_count": ingest.get("parent_count", 0),
                "indexed_count": ingest.get("indexed_count", ingest.get("chunk_count", 0)),
                "chunk_preview": ingest.get("chunk_preview"),
                "doc_hash": ingest.get("doc_hash", ""),
                "chunks": ingest.get("chunks"),
                "document": doc_json,
                "version": ver_json,
                "success": True,
            }
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("上传接口错误: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/documents/", include_in_schema=False)
    @app.get("/api/documents")
    async def documents_list(request: Request):
        try:
            active_agent = current_agent(request)
            docs = _jsonable(active_agent.list_documents())
            if hasattr(active_agent, "get_document"):
                enriched = []
                for doc in docs or []:
                    latest_metadata = {}
                    latest_content_chars = 0
                    latest_parser = ""
                    try:
                        document_id = str((doc or {}).get("id", "") or "")
                        if document_id:
                            latest = _jsonable(active_agent.get_document(document_id))
                            ver = (latest or {}).get("version") if isinstance(latest, dict) else None
                            if isinstance(ver, dict):
                                latest_metadata = ver.get("metadata") or {}
                                latest_content_chars = len(str(ver.get("content_md", "") or ""))
                                latest_parser = str((latest_metadata or {}).get("parser", "") or "")
                    except Exception:
                        latest_metadata = {}
                    item = dict(doc or {})
                    item["latest_metadata"] = latest_metadata
                    item["latest_content_chars"] = latest_content_chars
                    item["latest_parser"] = latest_parser
                    item["rag_chunk_count"] = 0
                    rag = getattr(active_agent, "rag", None)
                    if rag is not None and hasattr(rag, "document_chunk_count"):
                        try:
                            item["rag_chunk_count"] = int(rag.document_chunk_count(document_id) or 0)
                        except Exception:
                            item["rag_chunk_count"] = 0
                    enriched.append(item)
                docs = enriched
            return {"documents": docs}
        except Exception as e:
            logger.error("文档列表失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/documents/", include_in_schema=False)
    @app.post("/api/documents")
    async def documents_write(request: Request):
        try:
            active_agent = current_agent(request)
            payload = await request.json()
            res = active_agent.write_document(
                WriteRequest(
                    document_id=str((payload or {}).get("document_id", "") or ""),
                    title=str((payload or {}).get("title", "") or ""),
                    doc_type=str((payload or {}).get("doc_type", "") or ""),
                    source=str((payload or {}).get("source", "") or ""),
                    created_by=str((payload or {}).get("created_by", "") or ""),
                    content_md=str((payload or {}).get("content_md", "") or ""),
                    summary=str((payload or {}).get("summary", "") or ""),
                    metadata=(payload or {}).get("metadata") or {},
                ),
                bool((payload or {}).get("ingest_to_rag")),
            )
            out = _jsonable(res)
            if isinstance(out, dict) and "ingest" in out and not isinstance(out.get("ingest"), dict):
                version = out.get("version") or {}
                document = out.get("document") or {}
                out["ingest"] = _normalize_ingest_result(
                    out.get("ingest"),
                    str(version.get("content_md", "")) if isinstance(version, dict) else "",
                    document_id=str(document.get("id", "")) if isinstance(document, dict) else "",
                    version_id=str(version.get("id", "")) if isinstance(version, dict) else "",
                    section=str(document.get("doc_type", "")) if isinstance(document, dict) else "",
                )
            return out
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("文档写入失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/documents/{document_id}")
    async def documents_get(
        document_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50000, ge=1000, le=100000),
    ):
        try:
            out = _jsonable(current_agent(request).get_document(document_id))
            version = out.get("version") if isinstance(out, dict) else None
            if isinstance(version, dict):
                content = str(version.get("content_md", "") or "")
                total_chars = len(content)
                safe_offset = min(offset, total_chars)
                end = min(total_chars, safe_offset + limit)
                version["content_md"] = content[safe_offset:end]
                out["content_page"] = {
                    "offset": safe_offset,
                    "limit": limit,
                    "returned_chars": end - safe_offset,
                    "total_chars": total_chars,
                    "has_previous": safe_offset > 0,
                    "has_more": end < total_chars,
                }
            return out
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logger.error("读取文档失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/documents/{document_id}")
    async def documents_delete(document_id: str, request: Request):
        try:
            current_agent(request).delete_document(document_id)
            return {"ok": True, "document_id": document_id}
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logger.error("删除本地文档失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/documents/{document_id}/ingest")
    async def documents_ingest(document_id: str, request: Request):
        try:
            active_agent = current_agent(request)
            payload = {}
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            version_id = str((payload or {}).get("version_id", "") or "")
            # Re-ingesting a long document performs parsing, batch embedding and
            # index writes. Keep that synchronous pipeline off the event loop so
            # health checks, refresh and chat remain responsive while it runs.
            ingest_result = await run_in_threadpool(
                active_agent.ingest_document,
                document_id,
                version_id,
            )
            res = _jsonable(ingest_result)
            if not isinstance(res, dict):
                res = _normalize_ingest_result(
                    res,
                    "",
                    document_id=document_id,
                    version_id=version_id,
                    section="document",
                )
            return res
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("文档入库失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))
