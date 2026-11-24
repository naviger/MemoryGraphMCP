"""Provision Arango storage and the dedicated MemoryGraphMCP runtime account."""
from time import monotonic, sleep

from arango import ArangoClient
from requests.exceptions import RequestException

from app.config.settings import Settings
from app.repositories.factory import build_repository


def provision(settings: Settings, bootstrap_password: str) -> None:
    user = settings.arango_username
    if user != "memorygraphmcp_app" or not settings.arango_password:
        raise ValueError("Provisioning requires the dedicated memorygraphmcp_app account and its password")
    if not bootstrap_password or settings.arango_password == bootstrap_password:
        raise ValueError("Bootstrap and runtime passwords must be distinct")
    if settings.arango_database == "_system":
        raise ValueError("Use a dedicated application database")

    client = ArangoClient(hosts=settings.arango_url)
    system = client.db("_system", username="root", password=bootstrap_password)
    deadline = monotonic() + 60
    while True:
        try:
            system.properties()
            break
        except RequestException:
            if monotonic() >= deadline:
                raise
            sleep(1)

    db_name = settings.arango_database
    if not system.has_database(db_name):
        system.create_database(db_name)
    bootstrap = settings.model_copy(update={"arango_username": "root", "arango_password": bootstrap_password})
    storage = build_repository(bootstrap)
    storage.ensure_storage_ready()

    if not system.has_user(user):
        system.create_user(user, settings.arango_password)
    else:
        # The same environment file supplies the password to both services, including rotation.
        system.update_user(user, password=settings.arango_password, active=True)

    # This is a dedicated account. Remove old grants before installing the exact set.
    for database in system.permissions(user):
        system.reset_permission(user, database)
    system.update_permission(user, "none", "*")
    system.update_permission(user, "ro", db_name)
    system.update_permission(user, "none", db_name, "*")
    allowed = {
        settings.arango_documents_collection, settings.arango_edges_collection,
        settings.arango_vectors_collection, settings.arango_architecture_entities_collection,
        settings.arango_architecture_edges_collection, settings.arango_architecture_events_collection,
    }
    for entry in storage.database.collections():
        collection = entry["name"]
        if not collection.startswith("_"):
            system.update_permission(user, "rw" if collection in allowed else "none", db_name, collection)
    build_repository(settings).ensure_connected()


if __name__ == "__main__":
    from os import environ

    provision(Settings(), environ["MGMCP_ARANGO_BOOTSTRAP_PASSWORD"])
