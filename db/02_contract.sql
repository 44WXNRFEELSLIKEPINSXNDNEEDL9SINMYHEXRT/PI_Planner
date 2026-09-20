-- =====================================================================
--  ВЫХОД ПЛАНИРОВЩИКА — КОНТРАКТ С БЭКЕНДОМ
--  Эти таблицы пишет DS-слой, читает бэкенд. Структура заморожена:
--  если её нужно менять — сначала предупреди бэкендера, потом меняй.
--  Всё остальное (db/01_schema.sql) — внутренняя кухня ETL, бэкенду не нужна.
-- =====================================================================
BEGIN;

-- ---------------------------------------------------------------------
--  Прогон планировщика. Каждый пересчёт (раз в 2 недели) = новая строка.
--  Ничего не перезаписывается: история пересчётов видна целиком.
-- ---------------------------------------------------------------------
CREATE TABLE plan_runs (
    run_id       SERIAL PRIMARY KEY,
    pi_id        TEXT        NOT NULL REFERENCES pi_periods(pi_id),
    as_of_sprint SMALLINT    NOT NULL CHECK (as_of_sprint BETWEEN 0 AND 12),
    algorithm    TEXT        NOT NULL,
    params       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status       TEXT        NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','infeasible','failed')),
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN plan_runs.as_of_sprint IS '0 = базовый план на Неделе 0; 1..6 — пересчёт на начало соответствующего спринта.';
COMMENT ON COLUMN plan_runs.status IS 'infeasible = алгоритм не смог уложить бэклог даже с переносами. Фронту показывать явно, а не молча.';

-- ---------------------------------------------------------------------
--  Базовая линия Недели 0. Нужна для обоих KPI-числителей.
--  Фиксируется прогоном с as_of_sprint = 0 и дальше НЕ МЕНЯЕТСЯ.
-- ---------------------------------------------------------------------
CREATE TABLE plan_baseline (
    run_id     INT      NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
    task_id    TEXT     NOT NULL REFERENCES tasks(task_id),
    planned_sp NUMERIC(6,2) NOT NULL,
    committed  BOOLEAN  NOT NULL,
    PRIMARY KEY (run_id, task_id)
);
COMMENT ON TABLE plan_baseline IS
 'Обязательна: колонка «будет включено в спринт» в исходнике заполнена только у 8 задач Done, '
 'поэтому базовую линию строим сами (ADR-004). Без неё PI Predictability и Say/Do не считаются.';

-- ---------------------------------------------------------------------
--  Расписание задачи: в какие спринты попала и какое решение принято.
-- ---------------------------------------------------------------------
CREATE TABLE plan_task_schedule (
    run_id            INT      NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
    task_id           TEXT     NOT NULL REFERENCES tasks(task_id),
    start_sprint      SMALLINT,
    end_sprint        SMALLINT,
    forecast_end_date DATE,
    decision          TEXT     NOT NULL
        CHECK (decision IN ('in_quarter','deferred_next_pi','cancelled')),
    decision_reason   TEXT REFERENCES ref_mismatch_reasons(code),
    PRIMARY KEY (run_id, task_id),
    CHECK (decision <> 'in_quarter' OR (start_sprint IS NOT NULL AND end_sprint IS NOT NULL)),
    CHECK (end_sprint IS NULL OR start_sprint IS NULL OR end_sprint >= start_sprint)
);
COMMENT ON COLUMN plan_task_schedule.decision IS
 'in_quarter — влезает; deferred_next_pi — перенос («не будет взято в квартал»); cancelled — отменено заказчиком.';
COMMENT ON COLUMN plan_task_schedule.decision_reason IS 'Код из справочника причин расхождения. Для переносов заполнять обязательно — это объяснение для заказчика.';
CREATE INDEX ix_schedule_run_decision ON plan_task_schedule(run_id, decision);

-- ---------------------------------------------------------------------
--  Назначения: кто, на что, в каком спринте, сколько часов.
--  is_loan вычисляется СУБД — оранжевый алерт и метрика займов бесплатны.
-- ---------------------------------------------------------------------
CREATE TABLE plan_assignments (
    run_id          INT          NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
    task_id         TEXT         NOT NULL REFERENCES tasks(task_id),
    sprint_no       SMALLINT     NOT NULL CHECK (sprint_no BETWEEN 1 AND 12),
    engineer_id     TEXT         NOT NULL REFERENCES engineers(engineer_id),
    role_id         SMALLINT     NOT NULL REFERENCES roles(role_id),
    hours           NUMERIC(8,2) NOT NULL CHECK (hours > 0),
    home_team_id    TEXT         NOT NULL REFERENCES teams(team_id),
    serving_team_id TEXT         NOT NULL REFERENCES teams(team_id),
    is_loan         BOOLEAN      GENERATED ALWAYS AS (home_team_id IS DISTINCT FROM serving_team_id) STORED,
    PRIMARY KEY (run_id, task_id, sprint_no, engineer_id, role_id)
);
COMMENT ON COLUMN plan_assignments.home_team_id    IS 'Ядро, на орбите которого инженер отдал эти часы.';
COMMENT ON COLUMN plan_assignments.serving_team_id IS 'Ядро, которому принадлежит задача. Отличается от home → это заём (ADR-001).';
CREATE INDEX ix_assign_run_sprint ON plan_assignments(run_id, sprint_no);
CREATE INDEX ix_assign_engineer   ON plan_assignments(run_id, engineer_id);
CREATE INDEX ix_assign_loan       ON plan_assignments(run_id) WHERE is_loan;

-- ---------------------------------------------------------------------
--  ВРЕМЕННО́Й САТТЕЛИТ задачи: слепок состояния на каждый пересчёт.
--  Insert-only. Даёт «откат на спринт назад» и оба KPI.
-- ---------------------------------------------------------------------
CREATE TABLE task_state (
    run_id              INT      NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
    task_id             TEXT     NOT NULL REFERENCES tasks(task_id),
    as_of_sprint        SMALLINT NOT NULL CHECK (as_of_sprint BETWEEN 0 AND 12),
    status              TEXT     NOT NULL CHECK (status IN ('ToDo','InProgress','Done','Deferred','Cancelled')),
    remaining_hh        NUMERIC(8,2) NOT NULL CHECK (remaining_hh >= 0),
    remaining_sp        NUMERIC(6,2) NOT NULL CHECK (remaining_sp >= 0),
    forecast_end_sprint SMALLINT,
    PRIMARY KEY (run_id, task_id, as_of_sprint)
);
COMMENT ON TABLE task_state IS
 'Структурный саттелит — engineer_orbits (кто вокруг кого). Этот — временно́й (что когда было). '
 'Ядро tasks остаётся неизменяемым и описательным, вся динамика живёт здесь.';

-- ---------------------------------------------------------------------
--  Алерты. Три уровня из онбординга, один в один.
-- ---------------------------------------------------------------------
CREATE TABLE alerts (
    alert_id    SERIAL PRIMARY KEY,
    run_id      INT      NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
    sprint_no   SMALLINT NOT NULL,
    level       TEXT     NOT NULL CHECK (level IN ('red','yellow','orange')),
    alert_type  TEXT     NOT NULL CHECK (alert_type IN ('deadline_miss','cascade_shift','role_deficit')),
    entity_type TEXT     NOT NULL CHECK (entity_type IN ('task','initiative','team','role','engineer')),
    entity_id   TEXT     NOT NULL,
    message     TEXT     NOT NULL,
    payload     JSONB    NOT NULL DEFAULT '{}'::jsonb
);
COMMENT ON TABLE alerts IS
 'red/deadline_miss  — прогноз вылетает за 12-ю неделю, срыв инициативы PRODF;'
 ' yellow/cascade_shift — сдвиг по цепочке зависимостей, дедлайн пока цел;'
 ' orange/role_deficit  — потребность по роли на спринт > фонда доступных часов.';
CREATE INDEX ix_alerts_run ON alerts(run_id, level);

-- ---------------------------------------------------------------------
--  KPI. Нормы из онбординга лежат рядом со значением —
--  фронт красит плашку, не зашивая пороги у себя.
-- ---------------------------------------------------------------------
CREATE TABLE kpi_snapshots (
    run_id     INT      NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
    sprint_no  SMALLINT NOT NULL,
    kpi_code   TEXT     NOT NULL CHECK (kpi_code IN ('pi_predictability','say_do_ratio','bus_factor')),
    value      NUMERIC(8,2) NOT NULL,
    target_min NUMERIC(8,2),
    target_max NUMERIC(8,2),
    details    JSONB    NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, sprint_no, kpi_code)
);
COMMENT ON TABLE kpi_snapshots IS
 'Нормы: pi_predictability 80–100%, say_do_ratio 90–105%, bus_factor > 1. '
 'Для bus_factor значение = минимум по критическим технологиям, разбивка в details.';

COMMIT;
