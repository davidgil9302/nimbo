"""In-memory Google Drive and OAuth test fixtures.

Wire ``httpx.Client(transport=fake.transport())`` to the Google URLs, or use
``fake.client()`` for a client with the Drive base URL set. Register a bearer
token through ``fake.oauth.add_access_token("test-token")`` and send it in the
Authorization header. Seed files via ``add_folder``, ``add_file`` or
``add_native``, then inspect them through ``file``, ``find`` and ``snapshot``.

Register a ``Fault`` with ``fake.faults.add(...)`` to fail matching requests
before or after application. ``with fake.serve() as base:`` exposes the same
state and handlers over loopback for subprocess tests.
"""

from .api import FakeDrive
from .faults import CallRecord, Fault, FaultPlan, SimulatedCrash
from .state import FakeFile

__all__ = ["CallRecord", "FakeDrive", "FakeFile", "Fault", "FaultPlan", "SimulatedCrash"]
