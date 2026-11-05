-- The tables the policy system exports to Databricks (synthetic). Four typical exports:
--   assoc_dim                  associates and their resolved hierarchies (market, region, department, reporting line)
--   assoc_roles_dim            who holds which persona, with the scope values of that assignment
--   persona_column_policy      persona -> allowed (classification, data security category)
--   column_classification  qualified column name -> business term, classification, data security category
-- plus persona (each persona's scope type). Databricks only reads these tables; the policy system stays the source of truth.

-- 1. assoc_dim: one row per associate, hierarchies already resolved. reporting_chain = every manager above the
--    associate, nearest first. Story: 50002, 50003 and 50005 report to 50001; 50004 and 50006 report to 50005, so
--    50001 is two levels above them. Synthetic associate 20000000+k reports to 20000000+(k div 10), up the tree.
--    status: about 20% of the synthetic associates have left ('Terminated', the same ones that have a termination date
--    in associate_attrition); the story associates and the test users' wins (2000000x) stay 'Active'.
CREATE OR REPLACE TABLE {{CATALOG}}.{{EXPORTS}}.assoc_dim AS
SELECT win, employee_name, market, region, department,
       CASE WHEN win IN (50002, 50003, 50005) THEN array(CAST(50001 AS BIGINT))
            WHEN win IN (50004, 50006) THEN array(CAST(50005 AS BIGINT), CAST(50001 AS BIGINT))
            WHEN win < 20000000 THEN CAST(array() AS ARRAY<BIGINT>)
            ELSE transform(filter(sequence(1, 5), j -> (win - 20000000) DIV CAST(pow(10, j - 1) AS BIGINT) > 0),
                           j -> 20000000 + (win - 20000000) DIV CAST(pow(10, j) AS BIGINT)) END AS reporting_chain,
       CASE WHEN win >= 20000010 AND abs(hash(win, 7)) % 5 = 0 THEN 'Terminated' ELSE 'Active' END AS status
FROM {{CATALOG}}.{{BASE}}.associate_compensation;

-- 2. persona: the scope type of each persona (Enterprise, Market, Region, Department, Reporting or Self)
CREATE OR REPLACE TABLE {{CATALOG}}.{{EXPORTS}}.persona (persona_id STRING, persona_name STRING, scope_type STRING);
INSERT INTO {{CATALOG}}.{{EXPORTS}}.persona VALUES
  ('VP100', 'Market Manager',       'Market'),
  ('VP200', 'Regional HR Director', 'Region'),
  ('VP300', 'HR Business Partner',  'Department'),
  ('VP400', 'People Analyst',       'Enterprise'),
  ('VP500', 'People Manager',       'Reporting'),
  ('SELF',      'Self',                 'Self');
INSERT INTO {{CATALOG}}.{{EXPORTS}}.persona
SELECT concat('SYN_', lpad(cast(id AS STRING), 3, '0')), concat('Synthetic persona ', id),
       element_at(array('Market','Region','Department','Enterprise','Reporting'), cast(id % 5 + 1 AS INT))
FROM range(1, 201) AS t(id);

-- 3. persona_column_policy: the (classification, data security category) pairs each persona may see.
--    Non-Sensitive is visible to everyone who can see the row, so it is never listed.
CREATE OR REPLACE TABLE {{CATALOG}}.{{EXPORTS}}.persona_column_policy (persona_id STRING, classification STRING, data_security_category STRING);
INSERT INTO {{CATALOG}}.{{EXPORTS}}.persona_column_policy VALUES
  ('VP100','Sensitive','Compensation'), ('VP100','Sensitive','Employment'), ('VP100','Sensitive','Contact'),
  ('VP100','Highly Sensitive','Compensation'), ('VP100','Highly Sensitive','Payroll & Tax'),
  ('VP200','Sensitive','Compensation'), ('VP200','Sensitive','Employment'), ('VP200','Sensitive','Contact'),
  ('VP200','Highly Sensitive','Compensation'), ('VP200','Highly Sensitive','Payroll & Tax'), ('VP200','Highly Sensitive','Employment'),
  ('VP300','Sensitive','Employment'), ('VP300','Sensitive','Contact'),
  ('VP400','Sensitive','Compensation'), ('VP400','Sensitive','Organization'),
  ('VP500','Sensitive','Compensation'), ('VP500','Highly Sensitive','Compensation'), ('VP500','Sensitive','Employment'),
  ('VP500','Sensitive','Contact'),
  ('SELF','Sensitive','Compensation'), ('SELF','Sensitive','Employment'), ('SELF','Sensitive','Contact'),
  ('SELF','Highly Sensitive','Compensation'), ('SELF','Highly Sensitive','Payroll & Tax'), ('SELF','Highly Sensitive','Employment'),
  ('SELF','Highly Sensitive Restricted','Personal Identifiable');
INSERT INTO {{CATALOG}}.{{EXPORTS}}.persona_column_policy
SELECT p.persona_id, c.classification, c.data_security_category
FROM {{CATALOG}}.{{EXPORTS}}.persona p
CROSS JOIN VALUES ('Sensitive','Compensation'), ('Sensitive','Employment'), ('Sensitive','Contact'), ('Sensitive','Organization'),
                  ('Highly Sensitive','Compensation'), ('Highly Sensitive','Payroll & Tax'), ('Highly Sensitive','Employment')
                  AS c(classification, data_security_category)
WHERE p.persona_id LIKE 'SYN_%' AND abs(hash(p.persona_id, c.classification, c.data_security_category)) % 3 = 0;

-- 4. assoc_roles_dim: one row per (person, persona, scope value). user_principal is the person's Databricks login
--    (their identity provider email; for the POC's test users, their service principal's application ID). win is their employee ID.
--    Enterprise and Self assignments have no scope value. Reporting scope values are a manager's win.
CREATE OR REPLACE TABLE {{CATALOG}}.{{EXPORTS}}.assoc_roles_dim (user_principal STRING, win BIGINT, persona_id STRING, scope_value STRING, updated_at TIMESTAMP);
INSERT INTO {{CATALOG}}.{{EXPORTS}}.assoc_roles_dim VALUES
  ('{{SP:mm_market_abc}}',  50001,    'SELF',      NULL,       current_timestamp()),
  ('{{SP:mm_market_abc}}',  50001,    'VP100', 'Market A', current_timestamp()),
  ('{{SP:mm_market_abc}}',  50001,    'VP100', 'Market B', current_timestamp()),
  ('{{SP:mm_market_abc}}',  50001,    'VP100', 'Market C', current_timestamp()),
  ('{{SP:rhrd_south}}',     20000002, 'VP200', 'South',    current_timestamp()),
  ('{{SP:hrbp_hr}}',        20000003, 'VP300', 'HR',       current_timestamp()),
  ('{{SP:people_analyst}}', 20000004, 'VP400', NULL,       current_timestamp()),
  ('{{SP:people_mgr}}',    50005,    'SELF',      NULL,       current_timestamp()),
  ('{{SP:people_mgr}}',    50005,    'VP500', '50005',    current_timestamp()),
  ('{{SP:senior_mgr}}',       50001,    'SELF',      NULL,       current_timestamp()),
  ('{{SP:senior_mgr}}',       50001,    'VP500', '50001',    current_timestamp()),
  ('{{SP:multi_persona}}',  20000006, 'VP400', NULL,       current_timestamp()),
  ('{{SP:multi_persona}}',  20000006, 'VP100', 'Market A', current_timestamp()),
  ('{{SP:self_only}}',    50004,    'SELF',      NULL,       current_timestamp());
-- no_persona has no row at all
-- 2,000 synthetic users, each with Self and 1-5 synthetic personas, each persona with 1-8 scope values
INSERT INTO {{CATALOG}}.{{EXPORTS}}.assoc_roles_dim
SELECT concat('user', lpad(cast(u AS STRING), 4, '0'), '@example.com'), 20000100 + u, 'SELF', NULL, current_timestamp()
FROM range(1, 2001) AS t(u);
INSERT INTO {{CATALOG}}.{{EXPORTS}}.assoc_roles_dim
SELECT concat('user', lpad(cast(u AS STRING), 4, '0'), '@example.com'), 20000100 + u, persona_id,
       CASE scope_type
         WHEN 'Market' THEN concat('Market ', lpad(cast(abs(hash(u, k, j)) % 500 + 1 AS STRING), 4, '0'))
         WHEN 'Region' THEN element_at(array('North','South','East','West','Central'), cast(abs(hash(u, k, j)) % 5 + 1 AS INT))
         WHEN 'Department' THEN element_at(array('Eng','HR','Finance','Stores','Supply Chain','Merch','Marketing','Legal','IT','Ops','Pharmacy','eCommerce'), cast(abs(hash(u, k, j)) % 12 + 1 AS INT))
         WHEN 'Reporting' THEN cast(20000000 + abs(hash(u, k, j)) % 1000 AS STRING)
       END, current_timestamp()
FROM (
  SELECT a.u, a.k, p.persona_id, p.scope_type,
         explode(sequence(1, CASE WHEN p.scope_type = 'Enterprise' THEN 1 ELSE cast(abs(hash(a.u, a.k)) % 8 + 1 AS INT) END)) AS j
  FROM (SELECT u, explode(sequence(1, cast(u % 5 + 1 AS INT))) AS k FROM range(1, 2001) AS t(u)) a
  JOIN {{CATALOG}}.{{EXPORTS}}.persona p ON p.persona_id = concat('SYN_', lpad(cast(abs(hash(a.u, a.k)) % 200 + 1 AS STRING), 3, '0')));

-- 5. column_classification: the data catalog's mapping of each qualified column name (catalog.schema.table.column) to its
--    business term, classification and data security category. A column with no row here is unclassified
--    (associate_attrition.term_reason).
CREATE OR REPLACE TABLE {{CATALOG}}.{{EXPORTS}}.column_classification (
  qualified_column_name STRING, business_term STRING, classification STRING, data_security_category STRING);
INSERT INTO {{CATALOG}}.{{EXPORTS}}.column_classification VALUES
  ('{{CATALOG}}.{{BASE}}.associate_compensation.win',             'Associate ID',       'Non-Sensitive',               'Organization'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.employee_name',   'Employee Name',      'Non-Sensitive',               'Personal'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.email_address',   'Email Address',      'Non-Sensitive',               'Contact'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.market',          'Market',             'Non-Sensitive',               'Organization'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.region',          'Region',             'Non-Sensitive',               'Organization'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.department',      'Department',         'Non-Sensitive',               'Organization'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.job_code',        'Job Code',           'Sensitive',                   'Employment'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.base_salary',     'Base Salary',        'Sensitive',                   'Compensation'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.phone_number',    'Phone Number',       'Sensitive',                   'Contact'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.bonus',           'Bonus',              'Highly Sensitive',            'Compensation'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.tax_withholding', 'Tax Withholding',    'Highly Sensitive',            'Payroll & Tax'),
  ('{{CATALOG}}.{{BASE}}.associate_compensation.ssn',             'SSN',                'Highly Sensitive Restricted', 'Personal Identifiable'),
  ('{{CATALOG}}.{{BASE}}.associate_attrition.assoc_id',           'Associate ID',       'Non-Sensitive',               'Organization'),
  ('{{CATALOG}}.{{BASE}}.associate_attrition.mkt_cd',             'Market',             'Non-Sensitive',               'Organization'),
  ('{{CATALOG}}.{{BASE}}.associate_attrition.dept_nm',            'Department',         'Non-Sensitive',               'Organization'),
  ('{{CATALOG}}.{{BASE}}.associate_attrition.hire_dt',            'Hire Date',          'Sensitive',                   'Employment'),
  ('{{CATALOG}}.{{BASE}}.associate_attrition.term_dt',            'Termination Date',   'Highly Sensitive',            'Employment'),
  ('{{CATALOG}}.{{BASE}}.associate_attrition.rating',        'Performance Rating', 'Highly Sensitive',            'Employment'),
  ('{{CATALOG}}.{{BASE}}.market_summary.market',                  'Market',             'Non-Sensitive',               'Organization'),
  ('{{CATALOG}}.{{BASE}}.market_summary.department',              'Department',         'Non-Sensitive',               'Organization'),
  ('{{CATALOG}}.{{BASE}}.market_summary.headcount',               'Headcount',          'Sensitive',                   'Organization'),
  ('{{CATALOG}}.{{BASE}}.market_summary.total_salary',            'Total Salary',       'Highly Sensitive',            'Compensation');
