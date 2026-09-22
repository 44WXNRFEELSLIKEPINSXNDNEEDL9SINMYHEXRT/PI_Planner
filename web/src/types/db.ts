/**
 * СГЕНЕРИРОВАНО: tools/gen_types.py — руками не правим, правки затрёт перегенерация.
 * Источник: схема public живой базы PostgreSQL 17.
 * Перегенерация: uv run python tools/gen_types.py
 */

/** Значение json/jsonb из ДС. */
export type Json = string | number | boolean | null | Json[] | { [key: string]: Json }


/** таблица public.actual_uploads — 7 кол. · Одна загрузка = факт одного спринта. Повторная загрузка за тот же спринт заменяет прежнюю и все более поздние (иначе факт спринта 3 висел бы на старом факте спринта 2). */
export interface actual_uploads {
  upload_id: number;
  pi_id: string;
  sprint_no: number;
  source_file: string;
  source_sha256: string;
  uploaded_at: string;
  summary: Json;
}

/** таблица public.alerts — 9 кол. · red/deadline_miss  — прогноз вылетает за 12-ю неделю, срыв инициативы PRODF; yellow/cascade_shift — сдвиг по цепочке зависимостей, дедлайн пока цел; orange/role_deficit  — потребность по роли на спринт > фонда доступных часов. */
export interface alerts {
  alert_id: number;
  run_id: number;
  sprint_no: number;
  level: 'red' | 'yellow' | 'orange';
  alert_type: 'deadline_miss' | 'cascade_shift' | 'role_deficit';
  entity_type: 'task' | 'initiative' | 'team' | 'role' | 'engineer';
  entity_id: string;
  message: string;
  payload: Json;
}

/** таблица public.dq_issues — 7 кол. · Журнал находок ETL. Не блокирует загрузку — материал для слайда «что не так с исходными данными». */
export interface dq_issues {
  issue_id: number;
  batch_id: number;
  entity: string;
  entity_id: string | null;
  rule_code: string;
  severity: 'info' | 'warning' | 'error';
  detail: string;
}

/** таблица public.engineer_orbits — 3 кол. · ОРБИТА: привязка спутника к ядру со ставкой. 34 строки / 30 инженеров — 4 висят на двух орбитах (ENG-405, ENG-406, ENG-419, ENG-426). Политика часов — «орбита с приоритетом», см. ADR-001. */
export interface engineer_orbits {
  engineer_id: string;
  team_id: string;
  capacity_rate: number;
}

/** таблица public.engineer_skills — 2 кол. · Звёздная карта: заявленный стек. Развёрнут из skills_declared по запятой (см. ADR-006 про «Java, Core»). */
export interface engineer_skills {
  engineer_id: string;
  skill_id: number;
}

/** таблица public.engineers — 4 кол. · СПУТНИК. Роль, грейд и стек принадлежат инженеру, а не команде: проверено — у всех 4 парттаймеров атрибуты идентичны в обеих строках исходника. 30 уникальных инженеров из 34 строк листа. */
export interface engineers {
  engineer_id: string;
  role_id: number;
  grade: 'Junior' | 'Middle' | 'Senior';
  total_capacity_rate: number;
}

/** таблица public.initiatives — 4 кол. · Бизнес-инициатива заказчика. PRODF ↔ BR строго 1:1 (проверено на 15 инициативах). */
export interface initiatives {
  prodf_id: string;
  br_id: string;
  title: string | null;
  priority_rung: number | null;
}

/** таблица public.kpi_snapshots — 8 кол. · Нормы: pi_predictability 80–100%, say_do_ratio 90–105%, bus_factor > 1. ТЗ: «прогноз выполнения необходимо отличать от фактического результата» — kind = forecast (по плану) | actual (по загруженному факту). Формулы — ADR-023. */
export interface kpi_snapshots {
  run_id: number;
  sprint_no: number;
  kpi_code: 'pi_predictability' | 'say_do_ratio' | 'bus_factor';
  value: number;
  target_min: number | null;
  target_max: number | null;
  details: Json;
  kind: 'forecast' | 'actual';
}

/** таблица public.load_batches — 7 кол. · Один прогон ETL. sha256 исходного xlsx — чтобы видеть, на какой версии датасета считали. */
export interface load_batches {
  batch_id: number;
  source_file: string;
  source_sha256: string;
  etl_version: string;
  pi_start: string;
  loaded_at: string;
  row_counts: Json;
}

/** таблица public.pi_periods — 6 кол. · Границы PI (ADR-007). С 1.1.0 — КАЛЕНДАРНЫЙ квартал: 01.07..30.09.2026 (92 дня). Фонд ставки за весь PI = fte_hours_per_sprint × v_pi_fund_factor.factor = 525.71 ЧЧ. */
export interface pi_periods {
  pi_id: string;
  start_date: string;
  end_date: string;
  sprint_count: number;
  sprint_length_days: number;
  fte_hours_per_sprint: number;
}

/** таблица public.plan_assignments — 9 кол. */
export interface plan_assignments {
  run_id: number;
  task_id: string;
  sprint_no: number;
  engineer_id: string;
  role_id: number;
  hours: number;
  home_team_id: string;
  serving_team_id: string;
  is_loan: boolean | null;
}

/** таблица public.plan_baseline — 4 кол. · Обязательна: колонка «будет включено в спринт» в исходнике заполнена только у 8 задач Done, поэтому базовую линию строим сами (ADR-004). Без неё PI Predictability и Say/Do не считаются. */
export interface plan_baseline {
  run_id: number;
  task_id: string;
  planned_sp: number;
  committed: boolean;
}

/** таблица public.plan_runs — 9 кол. */
export interface plan_runs {
  run_id: number;
  pi_id: string;
  as_of_sprint: number;
  algorithm: string;
  params: Json;
  status: 'ok' | 'infeasible' | 'failed';
  note: string | null;
  actuals_upload_id: number | null;
  created_at: string;
}

/** таблица public.plan_task_schedule — 10 кол. */
export interface plan_task_schedule {
  run_id: number;
  task_id: string;
  start_sprint: number | null;
  end_sprint: number | null;
  forecast_end_date: string | null;
  decision: 'in_quarter' | 'deferred_next_pi' | 'cancelled';
  decision_reason: string | null;
  reason_code: string | null;
  reason_text: string | null;
  reason_details: Json;
}

/** таблица public.plan_task_sp — 4 кол. · Сумма долей по задаче = её estimation_sp. Инвариант SP_OVERFLOW суммирует доли по команде и спринту. */
export interface plan_task_sp {
  run_id: number;
  task_id: string;
  sprint_no: number;
  sp: number;
}

/** таблица public.ref_closure_results — 3 кол. */
export interface ref_closure_results {
  code: string;
  ord: number;
  label: string;
}

/** таблица public.ref_decision_reasons — 5 кол. · Почему задача включена, перенесена или отменена. Текст для конкретной задачи — plan_task_schedule.reason_text, разбивка — reason_details. legacy_reason — старый код из справочника расхождений (M2/M3/M4), оставлен для совместимости контракта. */
export interface ref_decision_reasons {
  code: string;
  ord: number;
  decision: 'in_quarter' | 'deferred_next_pi' | 'cancelled';
  label: string;
  legacy_reason: string | null;
}

/** таблица public.ref_mismatch_reasons — 3 кол. · Причины расхождения планов заказчика и исполнителя. В датасете v1 не используется (заказчик=исполнитель везде) — задел под UI согласования. */
export interface ref_mismatch_reasons {
  code: string;
  ord: number;
  label: string;
}

/** таблица public.ref_result_options — 3 кол. · Справочник «Варианты выбора цели» — в т.ч. статусы переноса/отмены для задач, не влезших в квартал. */
export interface ref_result_options {
  code: string;
  ord: number;
  label: string;
}

/** таблица public.role_aliases — 2 кол. · Разнописание ролей между блоками листа. В датасете v1: Девопс→ДевОпс, Разработчик IOS→Разработчик iOS, Разработчик BigData→Разработчик Big Data. Правится ДАННЫМИ, не кодом: добавь строку и перезалей. */
export interface role_aliases {
  alias: string;
  role_id: number;
}

/** таблица public.role_substitutions — 6 кол. · ВЗАИМОЗАМЕНЯЕМОСТЬ РОЛЕЙ. Кто может закрыть роль, которой в штате нет или не хватает. Это ВОЗМОЖНОСТЬ для планировщика, а не обязанность: алгоритм волен ею не пользоваться. Строки — решения, а не данные: каждую надо уметь защитить, поэтому rationale обязателен. См. ADR-009. */
export interface role_substitutions {
  required_role_id: number;
  covering_role_id: number;
  min_grade: 'Junior' | 'Middle' | 'Senior';
  efficiency: number;
  status: 'proposed' | 'confirmed' | 'rejected';
  rationale: string;
}

/** таблица public.roles — 3 кол. · Канонические ИТ-роли. Источник истины — строки матрицы сметы (блок Estimates). */
export interface roles {
  role_id: number;
  canonical_name: string;
  role_group: 'analysis' | 'development' | 'testing' | 'ops' | 'management' | 'support' | 'design' | 'other';
}

/** таблица public.skills — 3 кол. */
export interface skills {
  skill_id: number;
  name: string;
  normalized_name: string;
}

/** таблица public.sprints — 5 кол. · Генерится ETL из PI_START по PI_END (ADR-007, ADR-017). Датасет границы квартала явно не задаёт. Последний спринт обрезается по PI_END и потому короче: 23.09..30.09.2026 = 8 дней. */
export interface sprints {
  pi_id: string;
  sprint_no: number;
  start_date: string;
  end_date: string;
  length_days: number | null;
}

/** таблица public.task_actual_spent — 4 кол. · Часы, потраченные ЗА ЭТОТ спринт (не накопительно). Складываются по загрузкам. */
export interface task_actual_spent {
  upload_id: number;
  task_id: string;
  role_id: number;
  hours: number;
}

/** таблица public.task_actuals — 6 кол. */
export interface task_actuals {
  upload_id: number;
  task_id: string;
  status: 'ToDo' | 'InProgress' | 'Done';
  actual_start: string | null;
  actual_end: string | null;
  comment: string | null;
}

/** таблица public.task_dependencies — 4 кол. · Канонизировано как «A блокирует B» по заголовкам колонок листа. Все 3 типа (has to be done before / is required for / depends on) семантически одинаковы — проверено по смыслу задач, A везде предшествует B (ADR-003). */
export interface task_dependencies {
  blocking_task_id: string;
  blocked_task_id: string;
  raw_type: string;
  min_gap_sprints: number;
}

/** таблица public.task_role_estimates — 3 кол. · Матрица сметы 22×45, развёрнутая в long. Нулевые ячейки не хранятся. */
export interface task_role_estimates {
  task_id: string;
  role_id: number;
  hours: number;
}

/** таблица public.task_role_spent — 3 кол. · Факт по ролям (блок Spent_time_roles), только для 6 задач InProgress. Колонка tasks.spent_time у них пуста — остаток считается ТОЛЬКО отсюда, см. v_task_remaining_hh. */
export interface task_role_spent {
  task_id: string;
  role_id: number;
  hours: number;
}

/** таблица public.task_role_spent_seed — 3 кол. */
export interface task_role_spent_seed {
  task_id: string;
  role_id: number;
  hours: number;
}

/** таблица public.task_sequence — 5 кол. · Топологический порядок живого графа (Done отброшены), считается один раз при загрузке. Планировщик читает earliest_start_sprint как нижнюю границу и не пересчитывает граф на каждой итерации. ВАЖНО: живых рёбер всего 10 из 19, 27 из 37 задач свободны, глубина ≤2 — зависимости здесь НЕ узкое место. */
export interface task_sequence {
  task_id: string;
  topo_order: number;
  depth: number;
  earliest_start_sprint: number | null;
  on_critical_path: boolean;
}

/** таблица public.task_state — 7 кол. · Временно́й саттелит: состояние задачи на момент прогона. tasks — ТЕКУЩЕЕ состояние (воспроизводится из датасета и загрузок факта), а здесь — каким его видел каждый прогон. Инварианты сверяют прогон именно с этим слепком, а не с сегодняшним tasks. */
export interface task_state {
  run_id: number;
  task_id: string;
  as_of_sprint: number;
  status: 'ToDo' | 'InProgress' | 'Done' | 'Deferred' | 'Cancelled';
  remaining_hh: number;
  remaining_sp: number;
  forecast_end_sprint: number | null;
}

/** таблица public.tasks — 21 кол. */
export interface tasks {
  task_id: string;
  prodf_id: string;
  team_id: string;
  summary: string | null;
  status: 'ToDo' | 'InProgress' | 'Done';
  rung: number | null;
  estimation_sp: number | null;
  estimated_hh_effective: number;
  estimated_hh_declared: number | null;
  estimated_hh_matrix_total: number | null;
  spent_time_declared: number | null;
  created_at: string | null;
  planned_start: string | null;
  planned_end: string | null;
  actual_start: string | null;
  actual_end: string | null;
  result_planned: string | null;
  result_customer: string | null;
  result_executor: string | null;
  result_final: string | null;
  committed_week0: boolean;
}

/** таблица public.tasks_seed_state — 4 кол. · Состояние задач ровно как в загруженном датасете. Точка отсчёта для воспроизведения факта. */
export interface tasks_seed_state {
  task_id: string;
  status: string;
  actual_start: string | null;
  actual_end: string | null;
}

/** таблица public.team_history — 4 кол. · По 2 снимка на команду. Выборка мала — среднее velocity статистически шаткое, оговорить на защите. */
export interface team_history {
  team_id: string;
  snapshot_date: string;
  velocity_achieved: number;
  planned_sp: number;
}

/** таблица public.teams — 2 кол. · ЯДРО. Владеет только ёмкостью в Story Points. Часами владеют спутники (engineers). */
export interface teams {
  team_id: string;
  focus_factor: number;
}

/** вьюха public.v_backlog_demand — 5 кол. */
export interface v_backlog_demand {
  team_id: string | null;
  role_id: number | null;
  role_name: string | null;
  tasks: number | null;
  demand_hh: number | null;
}

/** вьюха public.v_bus_factor — 6 кол. · ПОКРЫТИЕ РОЛЕЙ: сколько инженеров на роль, включая роли без людей в штате (найм). Bus Factor по компетенциям, которого требует ТЗ, — v_bus_factor_skill (ADR-024). */
export interface v_bus_factor {
  role_id: number | null;
  role_name: string | null;
  role_group: string | null;
  bus_factor: number | null;
  demand_hh: number | null;
  risk: string | null;
}

/** вьюха public.v_bus_factor_skill — 10 кол. · Bus Factor по компетенциям (ТЗ): число инженеров, заявивших навык. Градация риска: «критично» — единственный носитель, который ещё и единственный специалист своей роли (выпал — работу не подхватит никто, замещения ролей запрещены); «единственный носитель» — навык у одного, но роль есть у других. in_demand — роль носителя нужна живому бэклогу. Покрытие ролей (роли без людей в штате) — отдельно: v_bus_factor, v_role_coverage_org. */
export interface v_bus_factor_skill {
  skill_id: number | null;
  skill_name: string | null;
  bus_factor: number | null;
  engineers: string[] | null;
  teams: string[] | null;
  roles: string[] | null;
  roles_demand_hh: number | null;
  in_demand: boolean | null;
  sole_in_role: boolean | null;
  risk: string | null;
}

/** вьюха public.v_dq_summary — 4 кол. */
export interface v_dq_summary {
  rule_code: string | null;
  severity: string | null;
  n: number | null;
  example: string | null;
}

/** вьюха public.v_engineer_absence_risk — 14 кол. · Профиль инженера + «что будет, если он выпадет» в данном прогоне. Замещения ролей запрещены организаторами (ADR-010), поэтому замена = другой инженер той же роли. */
export interface v_engineer_absence_risk {
  run_id: number | null;
  engineer_id: string | null;
  role_name: string | null;
  grade: string | null;
  total_capacity_rate: number | null;
  teams: string[] | null;
  role_bus_factor: number | null;
  unique_skills: string[] | null;
  unique_critical_skills: string[] | null;
  planned_hours: number | null;
  planned_tasks: string[] | null;
  tasks_without_backup: string[] | null;
  hours_without_backup: number | null;
  risk: string | null;
}

/** вьюха public.v_engineer_role_coverage — 7 кол. · Кто какую роль может закрывать. is_native=false — замещение, показывать в UI явно. efficiency — множитель часов (сейчас везде 1.00). Планировщик ВПРАВЕ игнорировать неродные строки. */
export interface v_engineer_role_coverage {
  engineer_id: string | null;
  role_id: number | null;
  role_name: string | null;
  is_native: boolean | null;
  efficiency: number | null;
  basis: string | null;
  status: string | null;
}

/** вьюха public.v_orbit_map — 10 кол. */
export interface v_orbit_map {
  engineer_id: string | null;
  role_name: string | null;
  role_group: string | null;
  grade: string | null;
  total_capacity_rate: number | null;
  orbit_count: number | null;
  teams: string[] | null;
  skills: string[] | null;
  bus_factor: number | null;
  risk: string | null;
}

/** вьюха public.v_pi_fund_factor — 4 кол. · Фонд ставки за весь PI, выраженный в «полных спринтах»: 1.0000 ставки × 80 ЧЧ × factor. На Q3-2026: 92 дня / 14 = 6.5714, то есть 525.71 ЧЧ за квартал (при 6 спринтах × 14 было 480). Используется вместо `sprint_count` везде, где считается фонд за квартал. */
export interface v_pi_fund_factor {
  pi_id: string | null;
  sprint_length_days: number | null;
  days_total: number | null;
  factor: number | null;
}

/** вьюха public.v_plan_assignment_detail — 12 кол. · is_substitution — инженер работает не по своей роли. Обязательно показывать в UI: «всё спланировалось» без ответа «кем» на защите не проходит. */
export interface v_plan_assignment_detail {
  run_id: number | null;
  task_id: string | null;
  sprint_no: number | null;
  engineer_id: string | null;
  hours: number | null;
  home_team_id: string | null;
  serving_team_id: string | null;
  is_loan: boolean | null;
  served_role: string | null;
  native_role: string | null;
  is_substitution: boolean | null;
  grade: string | null;
}

/** вьюха public.v_plan_diff — 16 кол. · Сравнение прогона с предыдущим: что изменилось (change_type) и почему (cause, explanation). Ответ на требование ТЗ «какие отклонения вызвали изменения». */
export interface v_plan_diff {
  run_id: number | null;
  prev_run_id: number | null;
  reported_sprint: number | null;
  task_id: string | null;
  prodf_id: string | null;
  team_id: string | null;
  prev_decision: string | null;
  prev_start: number | null;
  prev_end: number | null;
  new_decision: string | null;
  new_start: number | null;
  new_end: number | null;
  status_at_run: string | null;
  change_type: string | null;
  cause: string | null;
  explanation: string | null;
}

/** вьюха public.v_plan_violations — 5 кол. · Приёмка плана: нет строк с severity = error. Строки severity = warning план не отменяют, но требуют отображения в UI. Правила — docs/PLANNER_SPEC.md, раздел 7; разбор ревью M2 — docs/REVIEW_RESPONSE.md. */
export interface v_plan_violations {
  run_id: number | null;
  check_code: string | null;
  severity: string | null;
  entity: string | null;
  detail: string | null;
}

/** вьюха public.v_role_coverage_org — 7 кол. · Срез по всей компании: где нужен НАЙМ, а где хватит займов между командами. verdict=«только замещением» — роль держится исключительно на неродных исполнителях, это риск, показывать в UI. */
export interface v_role_coverage_org {
  role_name: string | null;
  demand_hh: number | null;
  native_people: number | null;
  people_incl_substitution: number | null;
  supply_hh: number | null;
  gap_hh: number | null;
  verdict: string | null;
}

/** вьюха public.v_role_deficit — 6 кол. */
export interface v_role_deficit {
  team_id: string | null;
  role_name: string | null;
  demand_hh: number | null;
  supply_hh: number | null;
  gap_hh: number | null;
  verdict: string | null;
}

/** вьюха public.v_role_deficit_effective — 6 кол. · Сравнивать с v_role_deficit (строгим). Разница между ними — ровно то, что даёт замещение. */
export interface v_role_deficit_effective {
  team_id: string | null;
  role_name: string | null;
  demand_hh: number | null;
  supply_with_substitution_hh: number | null;
  gap_hh: number | null;
  verdict: string | null;
}

/** вьюха public.v_role_supply_hh — 7 кол. · hh_per_sprint — фонд одного ПОЛНОГО спринта. hh_per_pi — фонд всего квартала: × v_pi_fund_factor.factor (92/14 = 6.5714), а НЕ × sprint_count, иначе короткий 7-й спринт подарил бы команде лишние 8 дней фонда. */
export interface v_role_supply_hh {
  role_id: number | null;
  role_name: string | null;
  team_id: string | null;
  engineers: number | null;
  fte: number | null;
  hh_per_sprint: number | null;
  hh_per_pi: number | null;
}

/** вьюха public.v_satellite_capacity — 12 кол. · hours_own — фонд спутника на орбите в КОНКРЕТНОМ спринте: rate × 80 × factor спринта. В коротком 7-м спринте это 0.5714 от обычного. */
export interface v_satellite_capacity {
  engineer_id: string | null;
  team_id: string | null;
  role_id: number | null;
  grade: string | null;
  pi_id: string | null;
  sprint_no: number | null;
  start_date: string | null;
  end_date: string | null;
  capacity_rate: number | null;
  is_shared_orbit: boolean | null;
  length_days: number | null;
  hours_own: number | null;
}

/** вьюха public.v_sprint_deviation — 12 кол. · По каждой загрузке факта: задачи, которые план держал в этом спринте, и что с ними на деле. «не закрыта в срок» — источник жёлтых и красных алертов следующего пересчёта. */
export interface v_sprint_deviation {
  upload_id: number | null;
  sprint_no: number | null;
  plan_run_id: number | null;
  task_id: string | null;
  team_id: string | null;
  estimation_sp: number | null;
  planned_start: number | null;
  planned_end: number | null;
  reported_status: string | null;
  planned_hours: number | null;
  spent_hours: number | null;
  deviation: string | null;
}

/** вьюха public.v_sprint_fund_factor — 6 кол. · Фонд спринта = rate × fte_hours_per_sprint × factor. Короткий спринт даёт МЕНЬШЕ часов, а не «те же 80»: иначе фонд квартала вылез бы за 92 дня календаря. Проверки ENGINEER_OVERLOAD и ORBIT_OVERLOAD берут фонд именно отсюда. */
export interface v_sprint_fund_factor {
  pi_id: string | null;
  sprint_no: number | null;
  start_date: string | null;
  end_date: string | null;
  length_days: number | null;
  factor: number | null;
}

/** вьюха public.v_task_board — 24 кол. */
export interface v_task_board {
  task_id: string | null;
  prodf_id: string | null;
  br_id: string | null;
  priority_rung: number | null;
  team_id: string | null;
  summary: string | null;
  status: string | null;
  rung: number | null;
  estimation_sp: number | null;
  estimated_hh_effective: number | null;
  estimated_hh_declared: number | null;
  estimated_hh_matrix_total: number | null;
  estimate_disputed: boolean | null;
  planned_start: string | null;
  planned_end: string | null;
  actual_start: string | null;
  actual_end: string | null;
  topo_order: number | null;
  depth: number | null;
  earliest_start_sprint: number | null;
  on_critical_path: boolean | null;
  remaining_hh: number | null;
  blocked_by: number | null;
  blocks: number | null;
}

/** вьюха public.v_task_remaining_hh — 5 кол. */
export interface v_task_remaining_hh {
  task_id: string | null;
  role_id: number | null;
  estimated_hours: number | null;
  spent_hours: number | null;
  remaining_hours: number | null;
}

/** вьюха public.v_team_capacity_sp — 6 кол. · history_points = 2 на команду: среднее шаткое, на защите оговорить. available_sp_per_sprint — фонд ОДНОГО ПОЛНОГО спринта; для короткого умножать на v_sprint_fund_factor.factor (так делает проверка SP_OVERFLOW). */
export interface v_team_capacity_sp {
  team_id: string | null;
  history_points: number | null;
  avg_velocity: number | null;
  focus_factor: number | null;
  available_sp_per_sprint: number | null;
  available_sp_per_pi: number | null;
}

/** вьюха public.v_team_profile — 15 кол. · Профиль команды для звёздной карты. roles_missing — роли, которые нужны бэклогу команды, но в ней нет ни одного инженера (закрываются займом или наймом). */
export interface v_team_profile {
  team_id: string | null;
  members: number | null;
  part_time_members: number | null;
  fte: number | null;
  hours_per_sprint: number | null;
  avg_velocity: number | null;
  available_sp_per_sprint: number | null;
  available_sp_per_pi: number | null;
  roles_present: string[] | null;
  roles_missing: string[] | null;
  skills_n: number | null;
  unique_skills: string[] | null;
  live_tasks: number | null;
  live_sp: number | null;
  live_hh: number | null;
}

/** Все отношения схемы public — таблицы и вьюхи. */
export type RelationName =
  | 'actual_uploads'
  | 'alerts'
  | 'dq_issues'
  | 'engineer_orbits'
  | 'engineer_skills'
  | 'engineers'
  | 'initiatives'
  | 'kpi_snapshots'
  | 'load_batches'
  | 'pi_periods'
  | 'plan_assignments'
  | 'plan_baseline'
  | 'plan_runs'
  | 'plan_task_schedule'
  | 'plan_task_sp'
  | 'ref_closure_results'
  | 'ref_decision_reasons'
  | 'ref_mismatch_reasons'
  | 'ref_result_options'
  | 'role_aliases'
  | 'role_substitutions'
  | 'roles'
  | 'skills'
  | 'sprints'
  | 'task_actual_spent'
  | 'task_actuals'
  | 'task_dependencies'
  | 'task_role_estimates'
  | 'task_role_spent'
  | 'task_role_spent_seed'
  | 'task_sequence'
  | 'task_state'
  | 'tasks'
  | 'tasks_seed_state'
  | 'team_history'
  | 'teams'
  | 'v_backlog_demand'
  | 'v_bus_factor'
  | 'v_bus_factor_skill'
  | 'v_dq_summary'
  | 'v_engineer_absence_risk'
  | 'v_engineer_role_coverage'
  | 'v_orbit_map'
  | 'v_pi_fund_factor'
  | 'v_plan_assignment_detail'
  | 'v_plan_diff'
  | 'v_plan_violations'
  | 'v_role_coverage_org'
  | 'v_role_deficit'
  | 'v_role_deficit_effective'
  | 'v_role_supply_hh'
  | 'v_satellite_capacity'
  | 'v_sprint_deviation'
  | 'v_sprint_fund_factor'
  | 'v_task_board'
  | 'v_task_remaining_hh'
  | 'v_team_capacity_sp'
  | 'v_team_profile'
