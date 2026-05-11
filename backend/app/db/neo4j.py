import time

from neo4j import GraphDatabase, Driver

from app.core.config import settings

driver: Driver | None = None


def init_neo4j() -> None:
    global driver
    last_error: Exception | None = None
    for _ in range(settings.db_init_retries):
        try:
            driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
            with driver.session() as session:
                session.run("CREATE CONSTRAINT landslide_id IF NOT EXISTS FOR (n:Landslide) REQUIRE n.id IS UNIQUE")
                session.run("CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE")
            return
        except Exception as exc:
            last_error = exc
            if driver is not None:
                driver.close()
            time.sleep(settings.db_init_retry_seconds)
    raise RuntimeError(f"Neo4j initialization failed: {last_error}") from last_error


def get_driver() -> Driver:
    if driver is None:
        raise RuntimeError("Neo4j is not initialized")
    return driver


def close_neo4j() -> None:
    if driver is not None:
        driver.close()
