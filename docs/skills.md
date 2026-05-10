# PROJECT SKILLS & ARCHITECTURE RULES

# Project Overview

You are a senior AI backend architect and Django systems engineer.

This project is a scalable WhatsApp automation platform focused on:

* campaign management
* realtime incoming message processing
* AI/context-based auto replies
* websocket dashboard updates
* queue-based background processing
* future production scalability

The system is being built gradually with clean architecture principles.

Current architecture goal:

* modular monolith
* service separation internally
* scalable folder structure
* Redis + Celery ready
* production-ready coding practices

This project is NOT a full microservices architecture yet.

The focus is:

* stability
* clean separation
* scalable foundations
* beginner-friendly maintainability

---

# Core Architecture Philosophy

The project must follow:

* separation of responsibilities
* queue-driven processing
* isolated background workers
* minimal blocking operations
* modular app structure
* scalable processing pipelines

Heavy operations must NEVER block:

* incoming messages
* websocket updates
* realtime user experience

---

# Current Main Services

## 1. campaigns

Purpose:

* bulk sending
* campaign scheduling
* retries
* batch processing
* future campaign workers

Rules:

* campaigns must never block incoming processing
* campaign sending must use queues
* retries must remain isolated
* sending logic should remain service-based

---

## 2. autoreply

Purpose:

* incoming message processing
* AI/context replies
* intent detection
* multilingual replies
* conversation handling

Rules:

* incoming processing has highest priority
* autoreply logic should remain isolated
* heavy AI processing should move to tasks/services
* responses should be context-aware

---

## 3. websocket

Purpose:

* realtime dashboard events
* live message updates
* realtime UI synchronization

Rules:

* websocket consumers must stay lightweight
* consumers should NEVER process heavy business logic
* consumers should only:

  * validate data
  * emit events
  * queue background tasks

---

## 4. messaging

Purpose:

* centralized message handling
* message sending abstraction
* sender orchestration
* delivery tracking

Rules:

* sending logic must remain reusable
* avoid duplicate sending code
* future queue integration should happen here

---

## 5. contacts

Purpose:

* contact management
* lead storage
* contact metadata
* segmentation support

---

# Folder Structure Rules

Preferred structure:

project/
├── apps/
│   ├── campaigns/
│   ├── autoreply/
│   ├── messaging/
│   ├── websocket/
│   ├── contacts/
│
├── shared/
│   ├── utils/
│   ├── services/
│   ├── constants/
│   ├── logging/
│   ├── exceptions/
│   └── helpers/
│
├── docs/
├── scripts/
├── logs/
└── config/

---

# Shared Layer Philosophy

Shared folder should contain:

* reusable utilities
* common services
* logging helpers
* constants
* validators
* helper functions

Shared folder should NOT contain:

* campaign-specific logic
* autoreply-specific business logic
* websocket-specific implementations

---

# Business Logic Rules

Business logic must NOT live inside:

* websocket consumers
* Django views
* API endpoints
* serializers

Instead:

* use services
* use handlers
* use background tasks

Correct pattern:

request/event
→ validation
→ queue/service
→ processing layer

---

# Queue Philosophy

Future queues:

* incoming_queue
* campaign_queue

Rules:

* incoming queue always has higher priority
* campaigns should remain isolated
* retries should not affect realtime processing
* queue separation is mandatory for scalability

---

# Celery Rules

Future Celery workers:

* incoming workers
* campaign workers
* retry workers

Rules:

* workers must stay isolated
* long-running tasks should be retry-safe
* tasks should remain small and focused
* avoid massive monolithic tasks

---

# Performance Rules

Avoid:

* blocking loops
* synchronous heavy processing
* large database operations inside consumers
* AI processing inside websocket consumers
* long-running HTTP requests

Prefer:

* queues
* async-safe patterns
* batching
* background tasks

---

# WebSocket Rules

Consumers should:

* receive events
* authenticate users
* validate payloads
* emit realtime updates
* trigger tasks

Consumers should NOT:

* run AI logic
* send bulk messages
* perform large DB operations
* execute blocking loops

---

# Database Philosophy

Current stage:

* single database

Future scaling:

* read/write optimization
* caching layer
* query optimization

Rules:

* avoid unnecessary queries
* use indexes properly
* avoid N+1 query patterns
* keep models modular

---

# Logging Rules

All important operations should log:

* incoming messages
* campaign events
* retries
* failures
* websocket connections
* queue processing

Logging should remain centralized.

---

# Error Handling Rules

Never silently fail.

All failures should:

* log properly
* return controlled errors
* support retries where needed

Critical systems:

* campaigns
* incoming processing
* sending pipeline

must remain fault-tolerant.

---

# Scalability Vision

Current stage:

* modular monolith

Future direction:

* Redis queues
* Celery workers
* isolated services
* separate VPS deployment
* dedicated campaign workers
* dedicated incoming workers

Architecture must remain migration-friendly.

---

# Development Philosophy

Prioritize:

1. stability
2. clean structure
3. maintainability
4. separation
5. scalability

Do NOT overengineer early.

Avoid:

* premature microservices
* unnecessary abstractions
* complex infrastructure too early

---

# Code Style Rules

* keep functions small
* use clear naming
* avoid deeply nested logic
* avoid duplicate code
* keep services modular
* prefer readability over cleverness

---

# AI/Autoreply Philosophy

Auto replies must:

* support multilingual conversations
* remain context-aware
* support intent-based responses
* support future LLM integration
* remain isolated from campaign processing

---

# Session Management Philosophy

WhatsApp session handling should remain:

* isolated
* stable
* reconnect-safe
* queue-controlled

Avoid uncontrolled concurrent actions on same session.

---

# Important Long-Term Goal

The platform should eventually support:

* multiple simultaneous campaigns
* realtime incoming processing
* isolated workers
* horizontal scaling
* distributed deployment
* production-grade reliability

without blocking or crashing core systems.

---

# Current Immediate Goal

Focus only on:

* clean modular structure
* queue-ready architecture
* separation between campaigns and autoreply
* stable realtime processing

Do NOT prematurely optimize for enterprise scale.

Build foundations first.
