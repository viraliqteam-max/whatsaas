# skills.md

# Production Architecture & Runtime Orchestration Rules

# Project Overview

You are a senior AI backend architect and distributed runtime systems engineer.

This project is a scalable browser orchestration and WhatsApp automation platform built using:

* Django backend
* GoLogin browser profiles
* Playwright automation
* Chrome extension agent
* WebSocket realtime communication
* Queue-driven background processing
* Runtime heartbeat synchronization
* AI/context-aware messaging
* Campaign processing pipelines

The platform is designed to support:

* multiple concurrent GoLogin runtimes
* realtime incoming message processing
* campaign broadcasting
* AI/autoreply systems
* scalable browser orchestration
* future distributed deployment

The architecture must prioritize:

1. stability
2. synchronization reliability
3. runtime ownership integrity
4. clean modular structure
5. queue isolation
6. scalable foundations
7. production-grade fault tolerance

---

# Core System Philosophy

This project is NOT a simple WhatsApp bot.

This project is evolving into:

* distributed browser runtime manager
* Playwright orchestration platform
* realtime automation infrastructure
* queue-driven messaging platform
* scalable browser session orchestrator

The system must remain:

* modular
* queue-driven
* reconnect-safe
* runtime-aware
* horizontally scalable
* fault-tolerant

---

# Core Architecture

Frontend UI
↓
Django API Layer
↓
Runtime Session Manager
↓
Queue / Task Layer
↓
GoLogin Launcher
↓
Chrome Browser
↓
Playwright Runtime Controller
↓
Extension Agent
↓
WebSocket Heartbeat Layer

---

# Runtime Source of Truth

The system must NEVER trust:

* frontend state
* button clicks
* stale DB values
* cached UI states

The ONLY source of truth is:

heartbeat

> Playwright browser connection
> extension websocket state
> browser PID alive
> DB state
> frontend UI

Frontend reflects runtime state.
Frontend does NOT determine runtime state.

---

# Core Runtime Rules

MANDATORY:

* one GoLogin profile = one active runtime
* one profile = one Playwright ownership
* one profile = one websocket ownership
* one profile = one heartbeat owner

Duplicate runtime ownership must NEVER happen.

---

# Mandatory Profile Locking

Profile locking is REQUIRED.

Before launching:

1. atomically acquire profile lock
2. create runtime_session_id
3. store runtime ownership
4. prevent duplicate launches

If:
profile.locked == True

Then:

* reject launch
* reject Playwright attach
* reject websocket ownership
* reject stale reconnect

---

# Correct Runtime Lifecycle

Launch requested
↓
Acquire lock
↓
Generate runtime_session_id
↓
status = launching
↓
Launch GoLogin browser
↓
Attach Playwright via connect_over_cdp
↓
Attach browser.on("disconnected")
↓
Extension websocket connects
↓
Heartbeat verification starts
↓
ONLY THEN:
status = active

IMPORTANT:
Never mark profile active immediately after launch request.

---

# Runtime Session Manager

The Runtime Session Manager is the central orchestration layer.

Responsibilities:

* runtime ownership tracking
* Playwright lifecycle tracking
* websocket ownership validation
* heartbeat synchronization
* stale runtime cleanup
* reconnect validation
* profile locking enforcement
* browser lifecycle management

---

# Runtime Registry

The in-memory runtime registry is the REAL runtime truth.

Example:

ACTIVE_PROFILES = {
profile_id: {
"playwright_browser": browser,
"context": context,
"page": page,
"pid": pid,
"runtime_session_id": uuid,
"last_heartbeat": timestamp,
"websocket_connected": True,
"playwright_connected": True,
}
}

The registry manages:

* runtime ownership
* active browser references
* heartbeat tracking
* cleanup lifecycle
* websocket ownership
* stale runtime prevention

---

# GoLogin Responsibility

GoLogin is ONLY responsible for:

* browser identity isolation
* fingerprint management
* profile launch infrastructure
* cookie/local storage isolation

GoLogin is NOT responsible for:

* runtime orchestration
* websocket lifecycle
* runtime ownership
* stale cleanup
* synchronization
* heartbeat validation

---

# Playwright Responsibility

Playwright is the runtime browser controller.

Responsibilities:

* connect_over_cdp
* browser lifecycle awareness
* DOM automation
* browser health verification
* disconnect detection
* runtime cleanup signals
* page lifecycle management

IMPORTANT:
Playwright becomes the primary browser-awareness layer.

---

# Critical Playwright Events

## browser.on("disconnected")

This is a CRITICAL runtime cleanup signal.

When triggered:

* unlock profile
* cleanup runtime registry
* disconnect websocket ownership
* stop heartbeat ownership
* remove ACTIVE_PROFILES entry
* deactivate profile
* notify frontend

## page.on("close")

Secondary browser closure verification.

---

# Heartbeat Architecture

Heartbeat is the PRIMARY runtime verification system.

Extension heartbeat interval:

* every 5 seconds

Example payload:

{
"type": "heartbeat",
"profile_id": "...",
"runtime_session_id": "...",
"timestamp": ...
}

Backend validates:

* runtime ownership
* websocket ownership
* runtime_session_id
* active runtime validity

Backend updates:

* last_heartbeat

---

# Heartbeat Timeout Rules

If no heartbeat received within 15 seconds:

MANDATORY ACTIONS:

* unlock profile
* status = inactive
* cleanup runtime registry
* disconnect websocket ownership
* cleanup Playwright references
* remove stale ownership
* notify frontend

Heartbeat timeout is the PRIMARY disconnect detector.

---

# Browser Close Detection

Browser closure must use MULTI-LAYER verification.

Priority order:

1. heartbeat timeout
2. browser.on("disconnected")
3. PID monitoring
4. page.on("close")
5. Playwright health checks

The system must NEVER rely on ONLY one detection method.

---

# PID Monitoring

Store browser_pid during launch.

Use:

* psutil.pid_exists(pid)

If PID dies:

* cleanup runtime
* unlock profile
* deactivate runtime
* cleanup ownership

PID monitoring is SECONDARY validation.
Heartbeat remains PRIMARY.

---

# Extension WebSocket Rules

Extension websocket connections must:

* identify profile_id
* identify runtime_session_id
* validate ownership
* reject stale reconnects
* prevent duplicate ownership

Reconnect logic must:

* reconnect safely
* stop heartbeat on disconnect
* reject stale ownership
* cleanup abandoned websocket ownership

Infinite "Reconnecting..." states must NEVER happen.

---

# Frontend Synchronization Rules

Frontend must NEVER assume:
launch clicked = active

Frontend state must ONLY come from backend runtime state.

Allowed states:

* launching
* browser_started
* playwright_connected
* extension_connected
* active
* reconnecting
* disconnected
* inactive
* crashed

Frontend updates must happen using:

* websocket updates
  OR
* polling fallback

Frontend must auto-update on:

* heartbeat timeout
* browser disconnect
* runtime cleanup
* Playwright disconnect
* stale ownership cleanup

---

# Modular Monolith Philosophy

The system currently follows:

* modular monolith architecture
* internal service separation
* queue-driven processing
* isolated workers
* scalable foundations

This is NOT full microservices yet.

Avoid premature overengineering.

---

# Core Apps

## campaigns

Responsibilities:

* bulk sending
* scheduling
* retries
* batching
* queue orchestration

Rules:

* campaigns must NEVER block incoming processing
* retries remain isolated
* sending logic must remain service-based

---

## autoreply

Responsibilities:

* incoming message processing
* AI/context replies
* multilingual support
* intent handling
* conversation management

Rules:

* incoming processing has highest priority
* AI logic must remain isolated
* heavy processing must move to tasks/services

---

## messaging

Responsibilities:

* centralized sending abstraction
* delivery tracking
* sender orchestration
* ACK synchronization
* retry management

Rules:

* avoid duplicate sending code
* sending logic must remain reusable
* queue integration belongs here

---

## websocket

Responsibilities:

* realtime dashboard updates
* runtime synchronization
* profile state updates
* live event streaming

Consumers must NEVER:

* run heavy logic
* run AI processing
* execute blocking operations
* process campaigns directly

Consumers should ONLY:

* validate
* emit events
* enqueue tasks

---

## contacts

Responsibilities:

* contact management
* segmentation
* metadata
* lead organization

---

# Queue Philosophy

Queues must remain isolated.

Future queues:

* incoming_queue
* campaign_queue
* retry_queue
* runtime_cleanup_queue
* websocket_event_queue

Rules:

* incoming processing has highest priority
* campaigns must remain isolated
* retries must NEVER block realtime flow

---

# Celery Philosophy

Future workers:

* incoming workers
* campaign workers
* retry workers
* runtime cleanup workers
* websocket event workers

Rules:

* workers remain isolated
* tasks remain retry-safe
* tasks remain small and focused
* avoid monolithic workers

---

# Business Logic Rules

Business logic must NEVER live inside:

* websocket consumers
* Django views
* serializers
* API endpoints

Correct flow:

request/event
↓
validation
↓
queue/service
↓
processing layer

---

# Shared Layer Rules

Shared folder contains:

* reusable services
* logging
* constants
* validators
* helpers
* runtime utilities
* queue helpers
* websocket helpers

Shared layer must NOT contain:

* app-specific business logic

---

# Database Philosophy

Current stage:

* single database

Future scaling:

* Redis caching
* read/write optimization
* query optimization
* connection pooling

Rules:

* avoid N+1 queries
* use indexes correctly
* avoid unnecessary queries
* keep models modular

---

# Runtime Cleanup Watcher

Watcher interval:

* every 10 seconds

Responsibilities:

* heartbeat timeout cleanup
* dead PID cleanup
* stale runtime cleanup
* orphan cleanup
* reconnect validation
* abandoned profile unlock

The watcher must survive:

* backend restart
* websocket restart
* Playwright crash
* stale ownership

---

# Concurrency Rules

All profile locks must be:

* atomic
* transaction-safe
* race-condition protected

Use:

* database transactions
* select_for_update
* runtime_session_id validation

Prevent:

* duplicate launches
* duplicate websocket ownership
* duplicate Playwright ownership
* stale runtime overwrite

---

# Logging Rules

All critical operations must log:

* launches
* disconnects
* heartbeat failures
* websocket connections
* retries
* cleanup operations
* runtime ownership changes
* stale cleanup events

Logging must remain centralized.

---

# Error Handling Rules

Never silently fail.

All failures must:

* log properly
* cleanup safely
* release ownership safely
* support retries when needed

Critical systems:

* runtime management
* messaging
* campaigns
* incoming processing

must remain fault-tolerant.

---

# Operational Rules

NEVER:

* trust frontend state
* mark active before heartbeat
* allow duplicate launches
* allow stale reconnect ownership
* run blocking operations in websocket consumers

ALWAYS:

* verify heartbeat
* validate ownership
* cleanup stale runtimes
* enforce profile locking
* attach Playwright disconnect listeners
* isolate heavy processing into workers/tasks

---

# Long-Term Vision

This platform should eventually support:

* multiple simultaneous campaigns
* distributed runtime orchestration
* isolated workers
* realtime incoming processing
* horizontal scaling
* multi-server deployments
* dedicated runtime orchestration services
* scalable Playwright browser infrastructure

without blocking or crashing critical systems.

---

# Immediate Development Goal

Focus ONLY on:

* stable runtime orchestration
* clean modular structure
* heartbeat synchronization
* profile locking correctness
* queue-ready architecture
* realtime synchronization
* stale runtime prevention
* scalable foundations

Build stable foundations first.
Do NOT prematurely optimize for enterprise-scale microservices.
