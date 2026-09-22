-- =====================================================================
--  ПочтаТех PI-Planner · нормализованное ядро (вариант B)
--  Схема: public.  Полный сброс — секция DROP ниже.
--  Порядок применения: 01_schema.sql → 02_contract.sql → build/seed.sql
--                      → 03_substitutions.sql → 04_views.sql
--  Решения и допущения: docs/DECISIONS.md
-- =====================================================================
BEGIN;

-- ---------- полный сброс (ETL идемпотентен, датасет ожидается v2) ----
DROP FUNCTION IF EXISTS apply_actuals() CASCADE;
DROP TABLE IF EXISTS plan_task_sp, kpi_snapshots, alerts, task_state, plan_assignments,
    plan_task_schedule, plan_baseline, plan_runs,
    task_actual_spent, task_actuals, actual_uploads, task_role_spent_seed, tasks_seed_state,
    ref_decision_reasons,
    dq_issues, task_sequence, sprints, pi_periods, team_history,
    task_dependencies, task_role_spent, task_role_estimates, tasks, initiatives,
    engineer_skills, engineer_orbits, engineers, teams,
    ref_closure_results, ref_mismatch_reasons, ref_result_options,
    role_substitutions, skills, role_aliases, roles, load_batches CASCADE;

-- =====================================================================
--  0. СЛУЖЕБНОЕ
-- =====================================================================
CREATE TABLE load_batches (
    batch_id      SERIAL PRIMARY KEY,
    source_file   TEXT        NOT NULL,
    source_sha256 TEXT        NOT NULL,
    etl_version   TEXT        NOT NULL,
    pi_start      DATE        NOT NULL,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_counts    JSONB       NOT NULL DEFAULT '{}'::jsonb
);
COMMENT ON TABLE  load_batches IS 'Один прогон ETL. sha256 исходного xlsx — чтобы видеть, на какой версии датасета считали.';
COMMENT ON COLUMN load_batches.row_counts IS 'Счётчики строк по каждой целевой таблице, для быстрой сверки после перезалива.';

-- =====================================================================
--  1. СПРАВОЧНИКИ
-- =====================================================================
CREATE TABLE roles (
    role_id        SMALLSERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    role_group     TEXT NOT NULL DEFAULT 'other'
        CHECK (role_group IN ('analysis','development','testing','ops','management','support','design','other'))
);
COMMENT ON TABLE roles IS 'Канонические ИТ-роли. Источник истины — строки матрицы сметы (блок Estimates).';

CREATE TABLE role_aliases (
    alias   TEXT     PRIMARY KEY,
    role_id SMALLINT NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE
);
COMMENT ON TABLE role_aliases IS
 'Разнописание ролей между блоками листа. В датасете v1: Девопс→ДевОпс, Разработчик IOS→Разработчик iOS, '
 'Разработчик BigData→Разработчик Big Data. Правится ДАННЫМИ, не кодом: добавь строку и перезалей.';

CREATE TABLE role_substitutions (
    required_role_id SMALLINT NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    covering_role_id SMALLINT NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    min_grade        TEXT     NOT NULL DEFAULT 'Middle' CHECK (min_grade IN ('Junior','Middle','Senior')),
    efficiency       NUMERIC(3,2) NOT NULL DEFAULT 1.00 CHECK (efficiency >= 1.00),
    status           TEXT     NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed','confirmed','rejected')),
    rationale        TEXT     NOT NULL,
    PRIMARY KEY (required_role_id, covering_role_id),
    CHECK (required_role_id <> covering_role_id)
);
COMMENT ON TABLE role_substitutions IS
 'ВЗАИМОЗАМЕНЯЕМОСТЬ РОЛЕЙ. Кто может закрыть роль, которой в штате нет или не хватает. '
 'Это ВОЗМОЖНОСТЬ для планировщика, а не обязанность: алгоритм волен ею не пользоваться. '
 'Строки — решения, а не данные: каждую надо уметь защитить, поэтому rationale обязателен. См. ADR-009.';
COMMENT ON COLUMN role_substitutions.status IS
 'proposed = наша гипотеза, НЕ подтверждена авторами задачи; confirmed = согласовано на контрольной точке; '
 'rejected = запрещено. Планировщик игнорирует rejected. Правится одним UPDATE после контрольной точки.';
COMMENT ON COLUMN role_substitutions.min_grade IS
 'Замещать может только инженер не ниже этого грейда. Для руководителя проекта — Senior.';
COMMENT ON COLUMN role_substitutions.efficiency IS
 'Во сколько раз замещающий тратит больше часов. 1.00 = без потерь. Сейчас у всех 1.00 намеренно: '
 'придумывать коэффициенты без основания не стали, ручка оставлена под ответ авторов задачи.';

CREATE TABLE skills (
    skill_id        SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE
);
COMMENT ON COLUMN skills.normalized_name IS 'lower() + схлопнутые пробелы. Матчинг стека идёт по нему, name — первое встреченное написание.';

CREATE TABLE ref_result_options   (code TEXT PRIMARY KEY, ord SMALLINT NOT NULL, label TEXT NOT NULL);
CREATE TABLE ref_mismatch_reasons (code TEXT PRIMARY KEY, ord SMALLINT NOT NULL, label TEXT NOT NULL);
CREATE TABLE ref_closure_results  (code TEXT PRIMARY KEY, ord SMALLINT NOT NULL, label TEXT NOT NULL);
COMMENT ON TABLE ref_result_options IS 'Справочник «Варианты выбора цели» — в т.ч. статусы переноса/отмены для задач, не влезших в квартал.';
COMMENT ON TABLE ref_mismatch_reasons IS 'Причины расхождения планов заказчика и исполнителя. В датасете v1 не используется (заказчик=исполнитель везде) — задел под UI согласования.';

-- =====================================================================
--  2. ЯДРА И СПУТНИКИ  (модель «команда-ядро + инженеры-спутники»)
-- =====================================================================
CREATE TABLE teams (
    team_id      TEXT PRIMARY KEY,
    focus_factor NUMERIC(3,2) NOT NULL DEFAULT 0.80 CHECK (focus_factor > 0 AND focus_factor <= 1)
);
COMMENT ON TABLE  teams IS 'ЯДРО. Владеет только ёмкостью в Story Points. Часами владеют спутники (engineers).';
COMMENT ON COLUMN teams.focus_factor IS 'Из онбординга = 0.8. Вынесен в колонку, а не в константу, чтобы можно было крутить по командам.';

CREATE TABLE engineers (
    engineer_id         TEXT PRIMARY KEY,
    role_id             SMALLINT     NOT NULL REFERENCES roles(role_id),
    grade               TEXT         NOT NULL CHECK (grade IN ('Junior','Middle','Senior')),
    total_capacity_rate NUMERIC(3,2) NOT NULL CHECK (total_capacity_rate > 0 AND total_capacity_rate <= 1)
);
COMMENT ON TABLE  engineers IS
 'СПУТНИК. Роль, грейд и стек принадлежат инженеру, а не команде: проверено — у всех 4 парттаймеров '
 'атрибуты идентичны в обеих строках исходника. 30 уникальных инженеров из 34 строк листа.';
COMMENT ON COLUMN engineers.total_capacity_rate IS 'Сумма ставок по всем орбитам. У всех парттаймеров = 1.00 (0.5+0.5).';

CREATE TABLE engineer_orbits (
    engineer_id   TEXT         NOT NULL REFERENCES engineers(engineer_id) ON DELETE CASCADE,
    team_id       TEXT         NOT NULL REFERENCES teams(team_id)         ON DELETE CASCADE,
    capacity_rate NUMERIC(3,2) NOT NULL CHECK (capacity_rate > 0 AND capacity_rate <= 1),
    PRIMARY KEY (engineer_id, team_id)
);
COMMENT ON TABLE engineer_orbits IS
 'ОРБИТА: привязка спутника к ядру со ставкой. 34 строки / 30 инженеров — 4 висят на двух орбитах '
 '(ENG-405, ENG-406, ENG-419, ENG-426). Политика часов — «орбита с приоритетом», см. ADR-001.';

CREATE TABLE engineer_skills (
    engineer_id TEXT NOT NULL REFERENCES engineers(engineer_id) ON DELETE CASCADE,
    skill_id    INT  NOT NULL REFERENCES skills(skill_id)       ON DELETE CASCADE,
    PRIMARY KEY (engineer_id, skill_id)
);
COMMENT ON TABLE engineer_skills IS 'Звёздная карта: заявленный стек. Развёрнут из skills_declared по запятой (см. ADR-006 про «Java, Core»).';

-- =====================================================================
--  3. БЭКЛОГ
-- =====================================================================
CREATE TABLE initiatives (
    prodf_id      TEXT PRIMARY KEY,
    br_id         TEXT NOT NULL UNIQUE,
    title         TEXT,
    priority_rung SMALLINT
);
COMMENT ON TABLE  initiatives IS 'Бизнес-инициатива заказчика. PRODF ↔ BR строго 1:1 (проверено на 15 инициативах).';
COMMENT ON COLUMN initiatives.priority_rung IS 'Скоринг инициативы = MAX(rung) её задач (ADR-005). У 7 из 15 rung внутри инициативы неоднороден.';

CREATE TABLE tasks (
    task_id                   TEXT PRIMARY KEY,
    prodf_id                  TEXT NOT NULL REFERENCES initiatives(prodf_id),
    team_id                   TEXT NOT NULL REFERENCES teams(team_id),
    summary                   TEXT,
    status                    TEXT NOT NULL CHECK (status IN ('ToDo','InProgress','Done')),
    rung                      SMALLINT,
    estimation_sp             SMALLINT,
    -- три источника трудозатрат; расходятся у 25 из 45 задач (ADR-002)
    estimated_hh_effective    NUMERIC(8,2) NOT NULL,
    estimated_hh_declared     NUMERIC(8,2),
    estimated_hh_matrix_total NUMERIC(8,2),
    spent_time_declared       NUMERIC(8,2),
    created_at                DATE,
    planned_start             DATE,
    planned_end               DATE,
    actual_start              DATE,
    actual_end                DATE,
    result_planned            TEXT REFERENCES ref_result_options(code),
    result_customer           TEXT REFERENCES ref_result_options(code),
    result_executor           TEXT REFERENCES ref_result_options(code),
    result_final              TEXT REFERENCES ref_closure_results(code),
    committed_week0           BOOLEAN NOT NULL DEFAULT FALSE
);
COMMENT ON COLUMN tasks.estimated_hh_effective IS
 'ВЫБРАННАЯ ИСТИНА = сумма столбца матрицы сметы (ADR-002). Именно она раскладывается по людям.';
COMMENT ON COLUMN tasks.committed_week0 IS
 'Колонка «будет включено в спринт». В датасете v1 заполнена ТОЛЬКО у 8 задач Done, поэтому для KPI '
 'непригодна — реальная базовая линия фиксируется в plan_baseline первым прогоном (ADR-004).';
CREATE INDEX ix_tasks_status  ON tasks(status);
CREATE INDEX ix_tasks_team    ON tasks(team_id);
CREATE INDEX ix_tasks_prodf   ON tasks(prodf_id);

CREATE TABLE task_role_estimates (
    task_id TEXT         NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    role_id SMALLINT     NOT NULL REFERENCES roles(role_id),
    hours   NUMERIC(8,2) NOT NULL CHECK (hours > 0),
    PRIMARY KEY (task_id, role_id)
);
COMMENT ON TABLE task_role_estimates IS 'Матрица сметы 22×45, развёрнутая в long. Нулевые ячейки не хранятся.';

CREATE TABLE task_role_spent (
    task_id TEXT         NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    role_id SMALLINT     NOT NULL REFERENCES roles(role_id),
    hours   NUMERIC(8,2) NOT NULL CHECK (hours >= 0),
    PRIMARY KEY (task_id, role_id)
);
COMMENT ON TABLE task_role_spent IS
 'Факт по ролям (блок Spent_time_roles), только для 6 задач InProgress. Колонка tasks.spent_time у них пуста — '
 'остаток считается ТОЛЬКО отсюда, см. v_task_remaining_hh.';

CREATE TABLE task_dependencies (
    blocking_task_id TEXT     NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    blocked_task_id  TEXT     NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    raw_type         TEXT     NOT NULL,
    min_gap_sprints  SMALLINT NOT NULL DEFAULT 1 CHECK (min_gap_sprints >= 1),
    PRIMARY KEY (blocking_task_id, blocked_task_id),
    CHECK (blocking_task_id <> blocked_task_id)
);
COMMENT ON TABLE task_dependencies IS
 'Канонизировано как «A блокирует B» по заголовкам колонок листа. Все 3 типа (has to be done before / '
 'is required for / depends on) семантически одинаковы — проверено по смыслу задач, A везде предшествует B (ADR-003).';

CREATE TABLE team_history (
    team_id           TEXT     NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    snapshot_date     DATE     NOT NULL,
    velocity_achieved NUMERIC(6,2) NOT NULL,
    planned_sp        NUMERIC(6,2) NOT NULL,
    PRIMARY KEY (team_id, snapshot_date)
);
COMMENT ON TABLE team_history IS 'По 2 снимка на команду. Выборка мала — среднее velocity статистически шаткое, оговорить на защите.';

-- =====================================================================
--  4. КАЛЕНДАРЬ PI
-- =====================================================================
CREATE TABLE pi_periods (
    pi_id              TEXT PRIMARY KEY,
    start_date         DATE     NOT NULL,
    end_date           DATE     NOT NULL,
    sprint_count       SMALLINT NOT NULL DEFAULT 7,
    sprint_length_days SMALLINT NOT NULL DEFAULT 14,
    fte_hours_per_sprint SMALLINT NOT NULL DEFAULT 80,
    CHECK (end_date > start_date)
);
CREATE TABLE sprints (
    pi_id      TEXT     NOT NULL REFERENCES pi_periods(pi_id) ON DELETE CASCADE,
    sprint_no  SMALLINT NOT NULL CHECK (sprint_no BETWEEN 1 AND 12),
    start_date DATE     NOT NULL,
    end_date   DATE     NOT NULL,
    -- Генерируемая: длина спринта — единственный источник множителя фонда
    -- (`v_pi_fund_factor`). Считать её руками в ETL нельзя — разъедется
    -- с датами, и фонд 7-го спринта станет «обычным» молча.
    length_days SMALLINT GENERATED ALWAYS AS ((end_date - start_date) + 1) STORED,
    PRIMARY KEY (pi_id, sprint_no),
    CHECK (end_date >= start_date)
);
COMMENT ON COLUMN pi_periods.fte_hours_per_sprint IS
 'Из онбординга: 1.0 ставки = 80 ЧЧ за 2-недельный спринт (уже с учётом Focus Factor). '
 'Лежит в данных, а не в коде вьюх, — чтобы менялось одной строкой.';
COMMENT ON TABLE pi_periods IS
 'Границы PI (ADR-007). С 1.1.0 — КАЛЕНДАРНЫЙ квартал: 01.07..30.09.2026 (92 дня). '
 'Фонд ставки за весь PI = fte_hours_per_sprint × v_pi_fund_factor.factor = 525.71 ЧЧ.';
COMMENT ON TABLE sprints IS
 'Генерится ETL из PI_START по PI_END (ADR-007, ADR-017). Датасет границы квартала явно не задаёт. '
 'Последний спринт обрезается по PI_END и потому короче: 23.09..30.09.2026 = 8 дней.';
COMMENT ON COLUMN sprints.length_days IS
 'Длина спринта в днях (включительно). Генерируемая колонка: ETL её не пишет. '
 'Множитель фонда = length_days / pi_periods.sprint_length_days.';

-- =====================================================================
--  5. ПРЕДРАСЧЁТ ГРАФА ЗАВИСИМОСТЕЙ
-- =====================================================================
CREATE TABLE task_sequence (
    task_id               TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    topo_order            INT      NOT NULL,
    depth                 SMALLINT NOT NULL,
    earliest_start_sprint SMALLINT,
    on_critical_path      BOOLEAN  NOT NULL DEFAULT FALSE
);
COMMENT ON TABLE task_sequence IS
 'Топологический порядок живого графа (Done отброшены), считается один раз при загрузке. Планировщик читает '
 'earliest_start_sprint как нижнюю границу и не пересчитывает граф на каждой итерации. '
 'ВАЖНО: живых рёбер всего 10 из 19, 27 из 37 задач свободны, глубина ≤2 — зависимости здесь НЕ узкое место.';

-- =====================================================================
--  6. КАЧЕСТВО ДАННЫХ
-- =====================================================================
CREATE TABLE dq_issues (
    issue_id  SERIAL PRIMARY KEY,
    batch_id  INT  NOT NULL REFERENCES load_batches(batch_id) ON DELETE CASCADE,
    entity    TEXT NOT NULL,
    entity_id TEXT,
    rule_code TEXT NOT NULL,
    severity  TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
    detail    TEXT NOT NULL
);
COMMENT ON TABLE dq_issues IS 'Журнал находок ETL. Не блокирует загрузку — материал для слайда «что не так с исходными данными».';
CREATE INDEX ix_dq_rule ON dq_issues(rule_code);

-- =====================================================================
--  7. ПРИЧИНЫ РЕШЕНИЙ ПЛАНИРОВЩИКА (ADR-022)
--  ТЗ: «объяснять причины включения, переноса и отмены задач».
--  Статичный справочник: живёт в схеме, ETL его не трогает.
-- =====================================================================
CREATE TABLE ref_decision_reasons (
    code          TEXT PRIMARY KEY,
    ord           SMALLINT NOT NULL,
    decision      TEXT     NOT NULL CHECK (decision IN ('in_quarter','deferred_next_pi','cancelled')),
    label         TEXT     NOT NULL,
    legacy_reason TEXT  -- без FK: seed.sql делает TRUNCATE ref_mismatch_reasons CASCADE и снёс бы справочник
);
COMMENT ON TABLE ref_decision_reasons IS
 'Почему задача включена, перенесена или отменена. Текст для конкретной задачи — '
 'plan_task_schedule.reason_text, разбивка — reason_details. legacy_reason — старый код '
 'из справочника расхождений (M2/M3/M4), оставлен для совместимости контракта.';

-- =====================================================================
--  8. ФАКТ СПРИНТОВ (ADR-021)
--  ТЗ: «раз в две недели пользователь загружает фактические результаты
--  очередного спринта». Загрузки — неизменяемый журнал; текущее состояние
--  tasks / task_role_spent ВОСПРОИЗВОДИТСЯ из снимка исходного датасета
--  плюс все загрузки по порядку (apply_actuals). Поэтому перезагрузка
--  факта за спринт безопасна: состояние пересобирается с нуля.
-- =====================================================================
CREATE TABLE tasks_seed_state (
    task_id      TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    actual_start DATE,
    actual_end   DATE
);
CREATE TABLE task_role_spent_seed (
    task_id TEXT         NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    role_id SMALLINT     NOT NULL REFERENCES roles(role_id),
    hours   NUMERIC(8,2) NOT NULL CHECK (hours >= 0),
    PRIMARY KEY (task_id, role_id)
);
COMMENT ON TABLE tasks_seed_state IS
 'Состояние задач ровно как в загруженном датасете. Точка отсчёта для воспроизведения факта.';

CREATE TABLE actual_uploads (
    upload_id     SERIAL PRIMARY KEY,
    pi_id         TEXT        NOT NULL REFERENCES pi_periods(pi_id) ON DELETE CASCADE,
    sprint_no     SMALLINT    NOT NULL CHECK (sprint_no BETWEEN 1 AND 12),
    source_file   TEXT        NOT NULL,
    source_sha256 TEXT        NOT NULL,
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (pi_id, sprint_no)
);
COMMENT ON TABLE actual_uploads IS
 'Одна загрузка = факт одного спринта. Повторная загрузка за тот же спринт заменяет прежнюю '
 'и все более поздние (иначе факт спринта 3 висел бы на старом факте спринта 2).';

CREATE TABLE task_actuals (
    upload_id    INT  NOT NULL REFERENCES actual_uploads(upload_id) ON DELETE CASCADE,
    task_id      TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    status       TEXT NOT NULL CHECK (status IN ('ToDo','InProgress','Done')),
    actual_start DATE,
    actual_end   DATE,
    comment      TEXT,
    PRIMARY KEY (upload_id, task_id)
);
CREATE TABLE task_actual_spent (
    upload_id INT          NOT NULL REFERENCES actual_uploads(upload_id) ON DELETE CASCADE,
    task_id   TEXT         NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    role_id   SMALLINT     NOT NULL REFERENCES roles(role_id),
    hours     NUMERIC(8,2) NOT NULL CHECK (hours >= 0),
    PRIMARY KEY (upload_id, task_id, role_id)
);
COMMENT ON TABLE task_actual_spent IS 'Часы, потраченные ЗА ЭТОТ спринт (не накопительно). Складываются по загрузкам.';

CREATE FUNCTION apply_actuals() RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
    -- 1. назад к датасету
    UPDATE tasks t
       SET status = s.status, actual_start = s.actual_start, actual_end = s.actual_end
      FROM tasks_seed_state s
     WHERE s.task_id = t.task_id;
    DELETE FROM task_role_spent;
    INSERT INTO task_role_spent (task_id, role_id, hours)
    SELECT task_id, role_id, hours FROM task_role_spent_seed;

    -- 2. статусы и даты: последнее слово — у самой поздней загрузки
    UPDATE tasks t
       SET status       = a.status,
           actual_start = COALESCE(a.actual_start, t.actual_start),
           actual_end   = CASE WHEN a.status = 'Done'
                               THEN COALESCE(a.actual_end, t.actual_end) END
      FROM (SELECT DISTINCT ON (ta.task_id) ta.*
              FROM task_actuals ta JOIN actual_uploads u ON u.upload_id = ta.upload_id
             ORDER BY ta.task_id, u.sprint_no DESC) a
     WHERE a.task_id = t.task_id;

    -- 3. часы копятся по всем загрузкам
    INSERT INTO task_role_spent (task_id, role_id, hours)
    SELECT task_id, role_id, SUM(hours) FROM task_actual_spent GROUP BY task_id, role_id
    ON CONFLICT (task_id, role_id) DO UPDATE SET hours = task_role_spent.hours + EXCLUDED.hours;
END
$fn$;
COMMENT ON FUNCTION apply_actuals() IS
 'Пересобирает текущее состояние задач: снимок датасета + все загрузки факта по порядку. '
 'Идемпотентна. Зовётся после каждой загрузки факта (app/actuals.py).';

COMMIT;

-- справочник причин — статичный, вне основной транзакции не нужен, но и
-- ETL его не перезаписывает: TRUNCATE в seed.sql его не касается.
INSERT INTO ref_decision_reasons (code, ord, decision, label, legacy_reason) VALUES
  ('PLANNED',              1, 'in_quarter',       'Включена в квартал: хватает ролей, часов и ёмкости команды', NULL),
  ('ROLE_NOT_IN_STAFF',    2, 'deferred_next_pi', 'В штате нет требуемой роли — нужен наём или дообучение',     'M2'),
  ('ROLE_HOURS_EXHAUSTED', 3, 'deferred_next_pi', 'Не хватает часов специалистов роли до конца квартала',      'M2'),
  ('TEAM_SP_EXHAUSTED',    4, 'deferred_next_pi', 'Не хватает ёмкости команды в Story Points',                 'M2'),
  ('BLOCKED_BY_DEFERRED',  5, 'deferred_next_pi', 'Ждёт задачу, которая сама перенесена',                      'M3'),
  ('INITIATIVE_ATOMIC',    6, 'deferred_next_pi', 'Перенесена вместе со своей инициативой',                    'M2'),
  ('PI_CLOSED',            7, 'deferred_next_pi', 'Квартал завершён — остаток уходит в следующий PI',          'M2'),
  ('NOT_FEASIBLE_NEXT_PI', 8, 'cancelled',        'Не помещается и в следующий квартал — рекомендуем отменить или пересогласовать', 'M4');
