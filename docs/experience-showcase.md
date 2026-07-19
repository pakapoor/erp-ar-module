# Enterprise Experience Showcase

---

## 1. Financial or ERP System and Business Impact

**System and role:**
At Meta (Feb 2021 – May 2023), I was a Software Engineer on the Commerce and Privacy Platforms team in London. I was personally accountable for architecting the creator affiliate payment pipeline and the privacy-driven decomposition of the Shops commerce platform — both directly protecting and enabling multi-billion-dollar revenue under GDPR and DMA regulation.

**Problem and scale:**
The creator affiliate payment pipeline needed to scale to millions of payments per month across regulated international markets, with full AML/KYC compliance per jurisdiction. Separately, the Shops platform had 1,000+ member variables spread across Shop, Catalog, and Product objects with unclear data ownership boundaries — a direct compliance liability under GDPR/DMA affecting multi-billion-dollar commerce revenue. Any data mis-classification risked regulatory action across the EU and UK markets.

**Technical contribution:**
For the payment pipeline, I designed the end-to-end architecture covering payment eligibility, hold periods, payout scheduling, AML/KYC gating, and CSV-based settlement — with idempotent write semantics to prevent duplicate payouts under retry. For the platform decomposition, I restructured data ownership across 1,000+ member variables without a DB-level migration — using a caller-context GraphQL API layer that returned correct data subsets across third-party, Shops, and ads invocations, enforcing privacy boundaries at the API governance layer rather than requiring schema changes.

**Business impact:**
The Click-to-WhatsApp CTA work I contributed to delivered a $500M+ annual revenue opportunity across carousel, video, and stories ad formats. The async catalog synchronization improvements achieved a 90% reduction in processing failures, and distributed test caching cut execution time by 54%, eliminating 99.9% of timeout failures. The GraphQL API governance pattern was adopted across the broader commerce ecosystem.

---

## 2. Data Integrity or Reconciliation Issue

**Failure observed:**
At Meta, the async catalog synchronization pipeline between Shops and the underlying product catalog was producing a high rate of processing failures — inconsistencies between what merchants had listed and what buyers could see, surfacing as stale or missing product data in active storefronts. The failure rate was high enough to affect merchant trust and required urgent remediation.

**Root cause:**
The root cause was a combination of missing transaction boundaries on catalog update events and timeout failures in the distributed test and sync infrastructure. Events were being partially applied — catalog metadata would update while inventory or pricing records would fail silently, leaving the catalog in an inconsistent intermediate state. Retries without idempotency controls compounded the problem by creating duplicate processing paths.

**Resolution:**
I introduced proper transaction boundaries around catalog update operations, ensuring that partial writes were rolled back rather than committed. Idempotency controls were added so that retry attempts replayed correctly rather than re-applying operations. The distributed test caching layer was redesigned to eliminate the timeout failure mode that was masking root causes during investigation.

**Evidence:**
Post-remediation metrics showed a 90% reduction in processing failures on the catalog synchronization pipeline. The distributed test caching change cut execution time by 54% and eliminated 99.9% of timeout failures — making the system reliable enough that the failure mode stopped recurring in production. These numbers were tracked in Meta's internal observability tooling and formed part of my performance review evidence.

---

## 3. Period Close, Audit, or Compliance

**Context:**
At Meta, the Shops platform operated under GDPR (EU) and DMA (Digital Markets Act) compliance obligations in regulated international markets. This was not a traditional accounting period close, but it was a continuous compliance audit posture — where data residency, access controls, data subject rights, and lawful basis for processing had to be demonstrably enforced at every API boundary, with audit lineage available on demand for regulatory review.

**Control challenge:**
The challenge was that data ownership across the commerce platform was implicit — 1,000+ member variables across Shop, Catalog, and Product objects had no clear classification of what was personal data, what was merchant data, what was user-behavioural data, and which legal entity owned it under which lawful basis. Privacy Legal required that this be resolvable at query time, with caller context determining what data was returned. Without this, any API call was potentially a data protection violation.

**Your action:**
I architected and led the privacy-driven decomposition of the platform — restructuring data ownership classification across the 1,000+ member variables and designing the unified GraphQL API layer with caller-context filtering. This meant a single API interface that returned the correct data subset depending on whether the caller was a third-party developer, a Shops surface, or an ads invocation — with the filtering enforced at the API governance layer, not left to individual callers. I drove XFN alignment across Privacy, Legal, Payments, Product, Integrity, and ML/Ranking teams spanning multiple organisations.

**Outcome:**
The decomposition protected multi-billion-dollar Shops commerce revenue from regulatory risk under GDPR/DMA without requiring a database-level migration. The API governance pattern was adopted as a reusable standard across the commerce ecosystem. The XFN delivery across Privacy and Legal stakeholders in regulated international markets was completed on schedule.

---

## 4. Multi-Tenant or Multi-Entity Data Modeling

**Context:**
At Lenovo (Aug 2019 – Jan 2021), I was the Technology Architect for Lenovo's commercial IoT platform serving 100M+ commercial devices — laptops, workstations, and ThinkPad/ThinkCentre devices deployed across enterprise customers globally. Each enterprise customer was effectively a tenant, with their own device fleet, telemetry data, alert policies, and analytics views. The platform needed strict isolation between customers while sharing the same Kafka/Flink processing infrastructure and analytics backend.

**Hard problem:**
The hard problem was multi-tenant isolation in a streaming analytics pipeline. Device telemetry at 150K events/sec had to be partitioned by tenant so that one enterprise customer's device data never leaked into another's analytics view, alert pipeline, or AI anomaly detection results. Flink's default state management was not keyed by tenant, causing state bleed across customer boundaries under high load — a correctness and compliance risk for enterprise customers with contractual data isolation requirements.

**Design:**
I redesigned the Flink state management layer with RocksDB-backed keyed partitioning — using a composite key of tenant ID plus device ID as the Flink key, ensuring that all state (aggregations, anomaly detection windows, alert thresholds) was physically isolated per tenant in RocksDB. This gave O(1) local state access per tenant key with zero cross-tenant state bleed. The Kafka topic partitioning strategy was aligned to the same tenant key so that events for a given tenant always landed on the same Flink partition. The Angular analytics frontend enforced tenant scope at the API layer — no cross-tenant query was expressible.

**Result:**
The redesign delivered P99 latency reduction from 500ms to 250ms (50% improvement) and 30% memory usage reduction, while achieving 99.9% availability at 150K events/sec across 100M+ connected commercial devices. Tenant isolation was verified through load testing that confirmed no state cross-contamination under concurrent multi-tenant load. This architecture informed the Tenant → Entity hierarchy in this ERP prototype — where tenant_id is carried in every JWT, enforced at the application layer on every query, and independently enforced through PostgreSQL RLS, so no cross-tenant data access is expressible at any layer.

---

## Interview Bridge to This Prototype

The multi-tenant keyed partitioning I designed at Lenovo directly informed the Tenant → Entity → Invoice hierarchy here — where tenant and entity identity come from the verified JWT rather than the request body, and PostgreSQL RLS provides a second independent enforcement layer. The catalog reconciliation work at Meta — where partial writes and missing transaction boundaries caused data inconsistencies — directly informed the single-transaction ACID design for payment allocation, the AR-to-GL reconciliation check in the health endpoint, and the idempotency controls that prevent duplicate financial writes under retry. The GDPR/DMA compliance work at Meta, where data ownership had to be provable at every API boundary, informed the audit trigger design here — where every financial mutation is captured at the database layer with verified actor identity, independent of application code paths.
