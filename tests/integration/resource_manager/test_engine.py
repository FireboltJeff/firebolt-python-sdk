import time

import pytest

from firebolt.service.manager import ResourceManager, Settings
from firebolt.service.types import EngineStatusSummary


@pytest.mark.skip(reason="manual test")
def test_create_start_stop_engine():
    rm = ResourceManager()
    name = f"integration_test_{int(time.time())}"

    engine = rm.engines.create(name=name)
    assert engine.name == name

    database = rm.databases.create(name=name)
    assert database.name == name

    engine.attach_to_database(database=database)
    assert engine.database == database

    engine = engine.start()
    assert (
        engine.current_status_summary
        == EngineStatusSummary.ENGINE_STATUS_SUMMARY_RUNNING
    )

    engine = engine.stop()
    assert engine.current_status_summary in {
        EngineStatusSummary.ENGINE_STATUS_SUMMARY_STOPPING,
        EngineStatusSummary.ENGINE_STATUS_SUMMARY_STOPPED,
    }


@pytest.mark.skip(reason="manual test")
def test_copy_engine():
    rm = ResourceManager()
    name = f"integration_test_{int(time.time())}"

    engine = rm.engines.create(name=name)
    assert engine.name == name

    engine.name = f"{engine.name}_copy"
    engine_copy = rm.engines._send_create_engine(
        engine=engine,
        engine_revision=rm.engine_revisions.get_by_key(engine.latest_revision_key),
    )
    assert engine_copy


def test_databases_get_many(rm_settings: Settings, database_name, engine_name):
    rm = ResourceManager(rm_settings)

    # get all databases, at least one should be returned
    databases = rm.databases.get_many()
    assert len(databases) > 0
    assert database_name in {db.name for db in databases}

    # get all databases, with name_contains
    databases = rm.databases.get_many(name_contains=database_name)
    assert len(databases) > 0
    assert database_name in {db.name for db in databases}

    # get all databases, with name_contains
    databases = rm.databases.get_many(attached_engine_name_eq=engine_name)
    assert len(databases) > 0
    assert database_name in {db.name for db in databases}

    # get all databases, with name_contains
    databases = rm.databases.get_many(attached_engine_name_contains=engine_name)
    assert len(databases) > 0
    assert database_name in {db.name for db in databases}
