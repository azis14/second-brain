"""
Vector database and RAG endpoints
"""
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Query
from typing import Optional
from pydantic import BaseModel
import logging
from vector_db import VectorDB
from services.rag_service import RAGService
from notion_client import AsyncClient
from utils.notion_utils import NotionUtils
import os
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from security import Secured

load_dotenv()
logger = logging.getLogger(__name__)

# Initialize services
vector_db = VectorDB()
rag_service = RAGService()

@asynccontextmanager
async def lifespan(app):
    """Handle startup and shutdown events"""
    # Startup: Initialize vector database
    try:
        await vector_db.ensure_vector_index()
        logger.info("Vector database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing vector database: {str(e)}")
    
    yield  # This is where FastAPI serves requests

router = APIRouter(prefix="/vector", tags=["vector"], lifespan=lifespan)

# Initialize Notion client for syncing
notion_api_key = os.getenv("NOTION_API_KEY")
notion_database_ids = os.getenv("NOTION_DATABASE_IDS", "").split(",") if os.getenv("NOTION_DATABASE_IDS") else []

if notion_api_key:
    notion = AsyncClient(auth=notion_api_key)
    notion_utils = NotionUtils(notion)

class SyncRequest(BaseModel):
    force_update: bool = True
    page_limit: Optional[int] = 100

def get_vector_db():
    return vector_db

def get_rag_service():
    return rag_service

def get_notion_client():
    return notion

def get_notion_utils():
    return notion_utils

@router.get("/stats", dependencies=[Secured])
async def get_vector_db_stats(db: VectorDB = Depends(get_vector_db)):
    """Get vector database statistics"""
    try:
        stats = await db.get_stats()
        return stats
    except Exception as e:
        logger.error(f"Error getting stats: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/sync", dependencies=[Secured])
async def sync_database(
    request: SyncRequest,
    background_tasks: BackgroundTasks,
    db: VectorDB = Depends(get_vector_db),
    client: AsyncClient = Depends(get_notion_client)
):
    """Sync entire Notion database to vector database"""
    try:
        # Start background sync task
        for database_id in notion_database_ids:
            background_tasks.add_task(
                _sync_database_background,
                database_id,
                request.force_update,
                request.page_limit,
                db,
                client
            )
        
        return {
            "status": "started",
            "message": "notion synced",
            "force_update": request.force_update
        }
    except Exception as e:
        logger.error(f"Error starting database sync: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

async def _sync_database_background(
    database_id: str,
    force_update: bool,
    page_limit: Optional[int],
    db: VectorDB,
    client: AsyncClient
):
    """Background task to sync database"""
    try:
        logger.info(f"Starting background sync for database {database_id}")
        
        # Get all pages from the database
        all_pages = []
        has_more = True
        next_cursor = None
        pages_processed = 0
        
        while has_more and (page_limit is None or pages_processed < page_limit):
            query_params = {
                "database_id": database_id,
                "page_size": min(100, page_limit - pages_processed if page_limit else 100)
            }
            if next_cursor:
                query_params["start_cursor"] = next_cursor
            
            response = await client.databases.query(**query_params)
            pages = response.get("results", [])
            all_pages.extend(pages)
            
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")
            pages_processed += len(pages)
            
            if page_limit and pages_processed >= page_limit:
                break

        
        logger.info(f"Found {len(all_pages)} pages to sync")
        
        # Sync each page
        sync_results = {
            "success": 0,
            "skipped": 0,
            "errors": 0,
            "total_chunks": 0
        }
        
        for page in all_pages:
            pageContent = []
            blocks_response = await notion.blocks.children.list(block_id=page["id"])
            for block in blocks_response.get("results", []):
                pageContent.append(notion_utils.extract_block_content(block))
            page["content"] = pageContent
            try:
                result = await db.store_notion_page(
                    page_id=page["id"],
                    page_data=page,
                    database_id=database_id,
                    force_update=force_update
                )
                
                if result["status"] == "success":
                    sync_results["success"] += 1
                    sync_results["total_chunks"] += result["chunks_stored"]
                elif result["status"] == "skipped":
                    sync_results["skipped"] += 1
                    
            except Exception as e:
                logger.error(f"Error syncing page {page['id']}: {str(e)}")
                sync_results["errors"] += 1
        
        logger.info(f"Database sync completed: {sync_results}")
        
    except Exception as e:
        logger.error(f"Error in background database sync: {str(e)}")

@router.get("/health", dependencies=[Secured])
async def vector_health_check(
    db: VectorDB = Depends(get_vector_db),
    rag: RAGService = Depends(get_rag_service)
):
    """Health check for vector database and RAG service"""
    try:
        # Test vector database connection
        stats = await db.get_stats()
        
        # Test embedding generation
        test_embedding = db.generate_embedding("test")
        
        return {
            "status": "healthy",
            "vector_db": "connected",
            "embedding_model": db.embedding_model_name,
            "embedding_dimension": len(test_embedding),
            "google_ai_model": rag.model_name,
            "total_chunks": stats.get("total_chunks", 0),
            "unique_pages": stats.get("unique_pages", 0)
        }
        
    except Exception as e:
        logger.error(f"Vector health check failed: {str(e)}")
        raise HTTPException(status_code=503, detail=f"Service unavailable: {str(e)}")

# Chat-like interface
@router.post("/chat", dependencies=[Secured])
async def chat_with_knowledge_base(
    question: str = Query(..., description="Your question"),
    rag: RAGService = Depends(get_rag_service)
):
    """Simple chat interface for asking questions"""
    try:
        answer = await rag.answer_question(
            question=question
        )
        
        # Format for chat-like response
        response = {
            "question": question,
            "answer": answer["answer"],
            "context_used": answer["context_used"],
            "sources_count": len(answer.get("sources", [])),
            "model": answer.get("model_used")
        }
        
        # Add source URLs if available
        if answer.get("sources"):
            response["source_urls"] = [
                source.get("page_url") for source in answer["sources"] 
                if source.get("page_url")
            ]
        
        return response
        
    except Exception as e:
        logger.error(f"Error in chat: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))