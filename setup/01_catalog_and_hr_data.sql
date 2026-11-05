-- The catalog, its five schemas, and the synthetic HR base tables (run as the admin).
-- hr_base holds the real tables. Nobody but the compiler (their owner) and the admin may read them: users read only the
-- generated secured views in hr_secure.
CREATE CATALOG IF NOT EXISTS {{CATALOG}} COMMENT 'FGAC POC: generated secured views (synthetic data only)';
CREATE SCHEMA IF NOT EXISTS {{CATALOG}}.{{BASE}} COMMENT 'HR base tables. Restricted: only the compiler (owner) and the admin.';
CREATE SCHEMA IF NOT EXISTS {{CATALOG}}.{{SECURE}} COMMENT 'Generated secured views (<table>_vw). The only schema users can read.';
CREATE SCHEMA IF NOT EXISTS {{CATALOG}}.{{EXPORTS}} COMMENT 'Tables exported by the policy system (source of truth). Restricted.';
CREATE SCHEMA IF NOT EXISTS {{CATALOG}}.{{GOV}} COMMENT 'Compiled entitlements and the compiler log. Restricted.';
CREATE SCHEMA IF NOT EXISTS {{CATALOG}}.{{AUDIT}} COMMENT 'Generated audit views over the system tables and the governance history. The admin and the auditors only.';

-- 1. associate_compensation: six story associates (50001-50006, used throughout docs/HOW_TO_RUN.md), plus
--    100,000 synthetic associates over 500 markets and 12 departments
CREATE OR REPLACE TABLE {{CATALOG}}.{{BASE}}.associate_compensation (
  win BIGINT, employee_name STRING, email_address STRING, job_code STRING, base_salary INT, bonus INT,
  phone_number STRING, market STRING, region STRING, department STRING, tax_withholding INT, ssn STRING);
INSERT INTO {{CATALOG}}.{{BASE}}.associate_compensation VALUES
  (50001, 'Alex Morgan', 'alex.morgan@example.com', 'ENG2', 131000, 16000, '201-555-0101', 'Market A', 'North', 'Eng', 1050, '900-10-0001'),
  (50002, 'Priya Shah',  'priya.shah@example.com',  'ENG3', 118000, 13000, '201-555-0102', 'Market B', 'North', 'Eng', 1250, '900-10-0002'),
  (50003, 'Daniel Kim',  'daniel.kim@example.com',  'ENG2', 124000, 11000, '201-555-0103', 'Market C', 'North', 'Eng', 1350, '900-10-0003'),
  (50004, 'Maria Lopez', 'maria.lopez@example.com', 'OPS1',  97000,  8500, '201-555-0104', 'Market D', 'South', 'HR',  1150, '900-10-0004'),
  (50005, 'Samuel Reed', 'samuel.reed@example.com', 'OPS3', 136000, 19000, '201-555-0105', 'Market E', 'South', 'HR',  1650, '900-10-0005'),
  (50006, 'Grace Chen',  'grace.chen@example.com',  'OPS2', 104000,  8500, '201-555-0106', 'Market F', 'South', 'HR',  1200, '900-10-0006');
INSERT INTO {{CATALOG}}.{{BASE}}.associate_compensation
SELECT 20000000 + id,
  concat(element_at(array('Ava','Liam','Noah','Emma','Olivia','Mason','Mia','Lucas','Zoe','Ethan'), cast(id % 10 + 1 AS INT)), ' ',
         element_at(array('Garcia','Lee','Patel','Nguyen','Johnson','Lopez','Kim','Martin','Clark','Young'), cast((id DIV 10) % 10 + 1 AS INT))),
  concat('assoc', id, '@example.com'),
  element_at(array('ENG1','ENG2','ENG3','MGR2','SAL1','SAL2','OPS1','HRBP'), cast(id % 8 + 1 AS INT)),
  cast(40000 + abs(hash(id)) % 1000 * 150 AS INT),
  cast(abs(hash(id, 1)) % 200 * 100 AS INT),
  concat(lpad(cast(id % 900 + 100 AS STRING), 3, '0'), '-', lpad(cast(id % 1000 AS STRING), 3, '0'), '-', lpad(cast(id % 10000 AS STRING), 4, '0')),
  concat('Market ', lpad(cast(id % 500 + 1 AS STRING), 4, '0')),
  element_at(array('North','South','East','West','Central'), cast(id % 5 + 1 AS INT)),  -- the market's region
  element_at(array('Eng','HR','Finance','Stores','Supply Chain','Merch','Marketing','Legal','IT','Ops','Pharmacy','eCommerce'), cast(id % 12 + 1 AS INT)),
  cast(abs(hash(id, 2)) % 50 * 40 AS INT),
  concat(lpad(cast(id % 900 + 100 AS STRING), 3, '0'), '-', lpad(cast(id % 90 + 10 AS STRING), 2, '0'), '-', lpad(cast(id % 10000 AS STRING), 4, '0'))
FROM range(0, 100000) AS t(id);

-- 2. associate_attrition: other physical column names (assoc_id, mkt_cd, dept_nm), to show that the rules follow
--    business terms, not column names. term_reason has no classification (an unclassified column).
CREATE OR REPLACE TABLE {{CATALOG}}.{{BASE}}.associate_attrition AS
SELECT win AS assoc_id, market AS mkt_cd, department AS dept_nm,
       date_sub(DATE'2026-10-01', cast(abs(hash(win)) % 5000 AS INT)) AS hire_dt,
       CASE WHEN abs(hash(win, 7)) % 5 = 0 OR win IN (50002, 50004) THEN date_sub(DATE'2026-10-01', cast(abs(hash(win, 8)) % 300 AS INT)) END AS term_dt,
       CASE WHEN abs(hash(win, 7)) % 5 = 0 OR win IN (50002, 50004)
            THEN element_at(array('Voluntary','Relocation','Performance','Retirement'), cast(abs(hash(win, 9)) % 4 + 1 AS INT)) END AS term_reason,
       element_at(array('Exceeds','Meets','Developing'), cast(abs(hash(win, 3)) % 3 + 1 AS INT)) AS rating
FROM {{CATALOG}}.{{BASE}}.associate_compensation;

-- 3. market_summary: a table with no associate key (one row per market and department), so only enterprise-wide
--    personas can see its sensitive columns
CREATE OR REPLACE TABLE {{CATALOG}}.{{BASE}}.market_summary AS
SELECT market, department, CAST(count(*) AS INT) AS headcount, CAST(sum(base_salary) AS BIGINT) AS total_salary
FROM {{CATALOG}}.{{BASE}}.associate_compensation GROUP BY market, department;
