# Local provider integration tests

The integration suite uses real HTTP clients with fake credentials and loopback
servers. It complements unit tests; it does not provision real machines or prove
GPU/bootstrap compatibility.

## Run

Requires Python 3.11+, a project virtualenv, Git and Node 22 (Node 18.20.1+ is
supported by the pinned Prism CLI). Docker and cloud credentials are not required.

```sh
sh scripts/setup_emulators.sh
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python scripts/test_emulators.py
```

Setup downloads test dependencies and DigitalOcean's official OpenAPI checkout.
The test run itself requires no cloud access. `PYTHON=python` can select an existing
Python environment for setup. Python test dependencies live in
`requirements-integration.txt`; Prism and transitive packages are pinned in the
integration `package.json` and lockfile. No emulator dependency is added to the
production Python requirements.

The runner starts servers on ephemeral `127.0.0.1` ports and shuts them down when
the suite finishes, including on failures. Journals and emulator logs use temporary
directories. The downloaded schema lives under ignored `.cache/emulators/` and
Node packages under ignored `tests/integration/node_modules/`; neither is copied
into the runtime image. Use the runner rather than invoking integration test files
directly so its network guard is active.

## What is exercised

| Backend | System under test | Coverage |
| --- | --- | --- |
| AWS | Real boto3 client → Moto 5.1.17 HTTP server | Templates, subnet/AZ discovery, Spot and On-Demand instant Fleet requests, ownership tags, inventory, termination, pool isolation |
| DigitalOcean | Real `DigitalOceanAPI` → stateful HTTP fixture → Prism 5.14.2 for create-schema validation | Actual provider create payload, deliberate schema rejection, delayed actions, pagination errors, quota rejection, response loss, ID recovery after restart, eviction and deletion |
| Azure | Real Compute/Network SDKs → stateful ARM HTTP fixture | Wire serialization, asynchronous create/delete polling, pagination, accepted creation with a failed reply, restart recovery, cleanup conflicts, eviction with leftover NIC/IP/disk |

DigitalOcean's schema is pinned to upstream revision
`6ee1980060d9ec8ce9f9853edf22768ca61cdf43`. The suite verifies the revision and refuses
modified tracked schema files. Prism independently checks the create request
against that schema. The stateful fixture supplies lifecycle behavior; Prism is
not a stateful Droplet emulator. Other fixture endpoints are not independently
validated against DigitalOcean's schema.

The Azure fixture is deliberately a small ARM simulator for our used operations,
not an Azure emulator. It leaves auxiliary resources after VM deletion so our
explicit cleanup path is exercised even when Azure's normal delete options would
remove them automatically. These tests do not verify Azure quota, IAM permissions,
image compatibility, real eventual-consistency timing or Spot capacity.

## Known Moto gap

Moto 5.1.17 does not apply the Fleet request's
`LaunchTemplateSpecificationUserData` to its simulated instance. The dedicated
test is marked `expectedFailure` and includes cleanup. This gap must remain visible
in the test report. An unexpected success fails the suite, prompting removal of
the marker after an emulator upgrade. Our existing botocore Stubber tests still
assert the exact production user-data request; we do not change AWS payloads to
match incomplete emulator behavior.

## Isolation and CI

The Python runner rejects non-loopback DNS lookups and socket connections. AWS
clients have explicit fake keys, no proxy and an explicit emulator endpoint. Azure
clients use a fake token provider and a test-only loopback authentication policy.
The DO endpoint override accepts only the production API origin or an explicit
loopback HTTP test server; pagination remains restricted to the selected origin.

Prism is a separate Node process started with a minimal environment (no inherited
cloud credentials) and a local pinned schema, in mock mode rather than proxy mode.
The Python network guard does not sandbox that subprocess; this is not a complete
OS network sandbox. All configured endpoints and schema references are local.

`.github/workflows/provider-tests.yml` runs unit and integration tests on pushes
and pull requests with read-only repository permissions and no cloud secrets.
It does not build/publish releases, deploy workloads, or operate Argo.

Live acceptance is still required for driver installation, k3s/Tailscale joining,
authenticated GHCR pulls, actual GPU workloads, real IAM/quota enforcement and
the providers' real resource deletion behavior.

References: [DO's Prism setup](https://github.com/digitalocean/openapi/blob/6ee1980060d9ec8ce9f9853edf22768ca61cdf43/docker-compose.yaml),
[Moto server mode](https://docs.getmoto.org/en/latest/docs/server_mode.html).
