# Module 7: Business Layer
**Owner:** Analytical Engineering (AE)
**Layer:** Business (aka Aggregation)
**Back to:** [claude.md](../claude.md)

---

## Purpose

The Business layer is the consumption-ready, business-facing data store. It aggregates, joins, and models Production data into structures directly usable by dashboards, reports, applications, and Ontology outputs. This is where business meaning is applied to clean data.

---

## Key Design Decisions

- All logic in SQL only
- Joins between Production tables happen here — not in Production
- Business layer tables are optimized for read performance
- Ontology outputs are mapped from Business layer tables
- The Business layer is rebuilt from Production — never directly from Staging

---

## Features to Build

### 7.1 Aggregation Scripts
- Pre-aggregated summary tables for common business metrics
- Configurable aggregation grain (daily, weekly, monthly, entity-level)
- Designed to reduce query load on Production for reporting consumers

```sql
-- Example: Daily transaction summary
SELECT
    transaction_date,
    source_system,
    status,
    COUNT(transaction_id)       AS total_transactions,
    SUM(amount)                 AS total_amount,
    AVG(amount)                 AS avg_amount,
    MIN(amount)                 AS min_amount,
    MAX(amount)                 AS max_amount
FROM production.erp_transactions
GROUP BY 1, 2, 3
```

### 7.2 Join Logic Automation
- Cross-source joins between Production tables
- Join keys defined in a join registry table
- Joins are SQL-based and auto-documented

```sql
-- Example: Customer enriched transactions
SELECT
    t.transaction_id,
    t.transaction_date,
    t.amount,
    t.status,
    c.customer_name,
    c.customer_segment,
    c.region
FROM production.erp_transactions t
LEFT JOIN production_crm_customers c
    ON t.customer_id = c.customer_id
```

### 7.3 Business Metric Definitions
- Standardized metric definitions stored in a metrics registry
- Each metric has: name, formula, grain, source tables
- Prevents different teams from calculating the same metric differently

```sql
CREATE TABLE metrics_registry (
    metric_id           VARCHAR,
    metric_name         VARCHAR,
    metric_formula      VARCHAR,
    grain               VARCHAR,   -- DAILY, WEEKLY, MONTHLY, ENTITY
    source_tables       VARCHAR,
    owner               VARCHAR,
    last_updated        TIMESTAMP,
    is_active           BOOLEAN
)
```

### 7.4 Ontology Output Mapping
- Business layer tables mapped to Foundry Ontology object types
- Object type properties mapped to Business layer table columns
- Link types defined for relationships between object types
- Configured via Pipeline Builder Ontology output node

### 7.5 Virtual Table Management
- High-frequency queries exposed as virtual tables
- Avoids recomputing expensive joins repeatedly
- Managed and versioned per consumer use case

### 7.6 Business Layer Table Management
- Naming convention: `business_{domain}_{subject}`
- Full rebuild from Production on each run (unless incremental configured)
- Schema documented in metrics registry

---

## Join Registry Table

```sql
CREATE TABLE join_registry (
    join_id             VARCHAR,
    join_name           VARCHAR,
    left_table          VARCHAR,
    right_table         VARCHAR,
    join_type           VARCHAR,   -- INNER, LEFT, RIGHT, FULL
    left_key            VARCHAR,
    right_key           VARCHAR,
    additional_conditions VARCHAR,
    owner               VARCHAR,
    is_active           BOOLEAN,
    last_updated        TIMESTAMP
)
```

---

## Business Layer Table Naming Convention

```
business_{domain}_{subject}

Examples:
  business_finance_transactions_summary
  business_customers_360
  business_sales_daily_performance
  business_operations_kpi
```

---

## Business Layer Flow

```
Production Tables
      ↓
Join Logic (join_registry driven)
      ↓
Business Metric Application
      ↓
Aggregation (grain-based)
      ↓
Business Layer Table Write
      ↓
Ontology Output Mapping
      ↓
Virtual Tables (for high-frequency queries)
```

---

## Dependencies

- Module 6: Production Layer (sole data source for the Business layer)
- Module 3: Schema Registry (for column references)
- Module 8: Orchestration (Business layer builds after Production completes)
- Module 9: Monitoring (Business layer freshness tracking)
- Module 10: Self-Service Interface (metric browsing for business users)
- Module 11: SQL Generation Engine (may generate join and aggregation SQL from the join/metrics registries)

---

## Open Questions

- [ ] Who owns metric definitions — AE team or business stakeholders?
- [ ] Do Business layer tables support incremental builds or always full rebuild?
- [ ] How do we version Business layer tables when metric definitions change?
- [ ] Which Business layer tables get mapped to Ontology vs remain as datasets?
