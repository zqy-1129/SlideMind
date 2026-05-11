from celery import Celery

from app.core.config import settings

celery_app = Celery("slidemind", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.task_routes = {"app.services.ingestion.ingest_file": {"queue": "imports"}}
celery_app.conf.imports = ("app.services.ingestion_tasks",)
celery_app.autodiscover_tasks(["app.services"])
