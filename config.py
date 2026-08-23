"""
Configuration constants for ATLAS (Automated Toolkit for Lightriver Asset & Systems).

Centralized location for all configuration values to make the system more maintainable
and easier to customize across different environments.
"""

# ============================================================================
# APPLICATION / UPDATE
# ============================================================================

# Keep this in lockstep with ATLAS.nsi `ProductVersion` so the installer and
# the running app agree on what's currently installed. The updater compares
# this against the latest GitHub release tag.
APP_VERSION = "2.0.10.0"

# GitHub release feed used by utils.update.Updater when running as an
# installed (frozen) build. Overridable via the LAMIS_UPDATE_REPO env var.
# The repo named here only needs to host the GitHub Releases — it doesn't
# have to be the source repo. Releases live in a dedicated public
# "Network-Inventory-Update" repo so source can stay wherever it lives
# while update distribution gets its own home.
GITHUB_OWNER = "Chees3loaf"
GITHUB_REPO = "Network-Inventory-Update"

# Filename of the installer asset attached to each release.
INSTALLER_ASSET_NAME = "ATLAS_Setup.exe"

# How long to wait on the GitHub API / asset download before giving up.
UPDATE_HTTP_TIMEOUT_S = 15

# ============================================================================
# NETWORK CONFIGURATION
# ============================================================================

# Pod/Network settings
#
# Pods live on 172.21.1xx.x: the third octet is POD_THIRD_OCTET_BASE + pod number,
# so Pod 1 is 172.21.101.x and Pod 13 is 172.21.113.x. The operator supplies only
# the fourth octet.
#
# The lab is a separate range on 10.9.x.x where the operator supplies BOTH the
# third and fourth octets, because the lab is not organised into pods.
POD_NETWORK_PREFIX = "172.21"         # first two octets for pod ranges
POD_THIRD_OCTET_BASE = 100            # Pod N -> third octet POD_THIRD_OCTET_BASE + N
POD_COUNT = 13                        # Pods 1..13 -> 172.21.101 .. 172.21.113
POD_MIN = POD_THIRD_OCTET_BASE + 1    # lowest pod third octet (101)
POD_MAX = POD_THIRD_OCTET_BASE + POD_COUNT  # highest pod third octet (113)

LAB_LABEL = "Lab"                     # dropdown entry that selects the lab range
LAB_NETWORK_PREFIX = "10.9"           # lab: 10.9.<operator>.<operator>

# Upper bound on a single Inventory sweep. A pod range is capped at 256 addresses
# by its fixed /24, but a Lab range takes a third octet at each end and can
# therefore cross /24 boundaries — this stops a mistyped octet from queueing a
# sweep of tens of thousands of addresses.
MAX_SCAN_ADDRESSES = 4096

# Default SSH/Telnet ports (standard, unlikely to change)
SSH_PORT = 22
TELNET_PORT = 23

# RLS Network Audit concurrency: number of SSH shells opened in parallel to
# the seed for neighbour collection. 1 = serial (historical behaviour);
# 3-5 fans the walk out across multiple shells so per-node REST sequences
# overlap, cutting a large multi-node walk roughly N-fold. Bounded in
# practice by the seed's concurrent-session limit (Ciena RLS typically
# allows a handful) -- if the seed rejects sessions the pool opens fewer
# and proceeds. Overridable per run via the RLS_AUDIT_SSH_POOL env var,
# which takes precedence (e.g. set it to 1 for a one-off serial run).
RLS_AUDIT_SSH_POOL_SIZE = 3

# ============================================================================
# AUTHENTICATION
# ============================================================================

# Credentials: ATLAS no longer persists user-entered credentials. The
# only stored creds are the Fernet-encrypted seed list (see
# _BUILTIN_DEFAULT_SEED in utils/credentials.py) which the auth-failure
# rotation cycles through on each device.

# SSH host-key trust policy.
# ATLAS is intended for unattended bulk operations across 100+ devices,
# where prompting the operator to accept each new SSH host key defeats
# the automation. By default we Trust-On-First-Use (TOFU): any unknown
# host key is silently recorded in the LAMIS known_hosts file the first
# time we see it, and enforced strictly thereafter (a *changed* key on a
# subsequent connection still raises and aborts — protecting against
# spoofing once the device is known).
#
# To restore interactive prompting, set env var LAMIS_PROMPT_HOSTKEYS=1
# or flip SSH_AUTO_ACCEPT_HOST_KEYS to False below.
SSH_AUTO_ACCEPT_HOST_KEYS = True

# ============================================================================
# TIMEOUT SETTINGS (in seconds)
# ============================================================================

# Connection timeouts
SSH_CONNECT_TIMEOUT = 10            # SSH connection timeout
TELNET_CONNECT_TIMEOUT = 10         # Telnet connection timeout
TELNET_READ_TIMEOUT = 5             # Telnet read operation timeout
SSH_READ_TIMEOUT = 5                # SSH read operation timeout

# Command execution timeouts
TDS_TIMEOUT = 1800                  # TDS diagnostics timeout (30 minutes)

# ============================================================================
# GUI SETTINGS
# ============================================================================

# Queue polling interval (milliseconds)
QUEUE_POLL_INTERVAL_MS = 100

# Loading screen dimensions
LOADING_SCREEN_WIDTH = 800
LOADING_SCREEN_HEIGHT = 600

# Main window dimensions
MAIN_WINDOW_WIDTH = 900
MAIN_WINDOW_HEIGHT = 750

# ============================================================================
# EXCEL/EXPORT SETTINGS
# ============================================================================

# Template paths (relative to project root)
DEVICE_REPORT_TEMPLATE = "data/Device_Report_Template.xlsx"
PACKING_SLIP_TEMPLATE = "data/ATLAS_Packing_Slip.xlsx"

# Excel column settings
EXCEL_MIN_COLUMN_WIDTH = 10
EXCEL_MAX_COLUMN_WIDTH = 60

# Summary sheet styling
CAPTURE_TIME_FONT_COLOR = "00FF00"      # Green font
CAPTURE_TIME_BG_COLOR = "000000"        # Black background
CAPTURE_TIME_BOLD = True

# Summary sheet starting row for device data
SUMMARY_DATA_START_ROW = 10

# Device sheet starting row for inventory data
DEVICE_DATA_START_ROW = 15

# ============================================================================
# DATABASE SETTINGS
# ============================================================================

# Database file location (relative to project root)
INVENTORY_DB_FILE = "data/network_inventory.db"

# Part number search settings
PART_NUMBER_PREFIX_LENGTH = 10      # Use first N characters of part number for DB lookup

# ============================================================================
# AI DOC ASSISTANT (Phase 1: cited Q&A over vendor docs)
# ============================================================================

# Master switch. When False, ATLAS behaves exactly as before and never
# imports the optional AI dependencies (openai, pypdf). Flip to True once
# a key/endpoint is configured.
AI_ASSISTANT_ENABLED = True

# Provider endpoint + credential.
#   - Personal/prototyping: leave AI_BASE_URL = None and set OPENAI_API_KEY
#     in the environment so the key never lives in the repo or the binary.
#   - Company rollout: point AI_BASE_URL at the server-side proxy that holds
#     the real key; the field laptops then carry no secret at all.
# Both are overridable at runtime via the LAMIS_AI_BASE_URL / OPENAI_API_KEY
# environment variables (see utils/ai/provider.py).
AI_BASE_URL = None

# Models. gpt-4o-mini is the cost/quality sweet spot for grounded RAG — the
# model only has to read the retrieved chunks, not know the docs.
AI_CHAT_MODEL = "gpt-4o-mini"
AI_EMBED_MODEL = "text-embedding-3-small"
AI_EMBED_DIM = 1536  # dimensionality of text-embedding-3-small

# Dense customer route diagrams need substantially stronger vision and a much
# larger structured-output budget than grounded text RAG. Keep this profile
# separate so diagram accuracy improvements do not raise the cost of every
# documentation-assistant request. ``auto`` lets current vision models retain
# native image detail when useful. High reasoning is used here because route
# topology, wrapped connectors, and dense labels require the strongest review
# pass before ATLAS stages a transcription.
RLS_DIAGRAM_MODEL = "gpt-5.6-terra"
RLS_DIAGRAM_IMAGE_DETAIL = "auto"
RLS_DIAGRAM_REASONING_EFFORT = "high"
RLS_DIAGRAM_MAX_COMPLETION_TOKENS = 65_536

# Retrieval / chunking knobs.
AI_CHUNK_WORDS = 220          # ~target words per chunk
AI_CHUNK_OVERLAP_WORDS = 40   # overlap so a procedure split across a boundary survives
AI_TOP_K = 6                  # chunks retrieved per question (ranked seeds)
# Neighbor expansion: each retrieved chunk is widened by this many adjacent
# chunks on each side (same doc) before being shown to the model. A single
# ~220-word chunk often holds only part of a multi-command procedure; pulling
# neighbors keeps prerequisite/setup steps (e.g. OSPF instance creation)
# together with the steps that reference them. 0 disables expansion.
AI_CONTEXT_WINDOW = 2
# Hybrid retrieval: in addition to the vector top_k, force in up to this many
# chunks that contain an exact salient term from the question (e.g. a
# hyphenated token like 'network-type', an acronym like 'OSPF', a part/version
# string). Vector similarity alone can rank an authoritative command chunk just
# below the cut; an exact-keyword guarantee surfaces it. 0 disables.
AI_KEYWORD_K = 8
# HyDE (Hypothetical Document Embeddings): before retrieval, draft a throwaway
# hypothetical answer and embed THAT alongside the question. A config/command
# block is semantically unlike a prose question, so plain retrieval misses it;
# a hypothetical that contains command-like text matches the real command pages.
# The hypothetical is used ONLY to steer retrieval and is never shown, so its
# syntax doesn't need to be correct. Costs one extra cheap LLM call per query.
AI_HYDE_ENABLED = True
# Fixed seed for the chat calls (HyDE draft + answer). With temperature 0 this
# makes the same question reproduce the same retrieval and answer run-to-run
# (best-effort — OpenAI doesn't fully guarantee seeded determinism, but it
# removes the bulk of the variance). Set to None to allow run-to-run variety.
AI_SEED = 7

# Doc index DB lives beside the inventory DB under %APPDATA%\ATLAS.
AI_INDEX_DB_FILE = "doc_index.db"

# ============================================================================
# LOGGING SETTINGS
# ============================================================================

# Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
# Default INFO; set to DEBUG only for troubleshooting (may contain device output).
LOG_LEVEL = "INFO"

# Log format
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

# Suppress verbose library logs
PIL_LOG_LEVEL = "WARNING"

# Serial-console diagnostics. When True, raises the ``atlas.serial``
# logger to DEBUG so every read/write through ``utils.serial_helpers``
# leaves a per-chunk byte transcript in the run log (hex + UTF-8
# repr). Useful when a baud probe fails or a login is silently looping.
# Independent of LOG_LEVEL so the rest of ATLAS stays at INFO. Default
# False — the always-on INFO-level hex dump on probe failure is
# usually enough to diagnose cable / baud / prompt-shape issues.
SERIAL_DEBUG = False

# Rotating log handler — bound on-disk footprint.
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 5             # Keep the latest 5 rotated files
