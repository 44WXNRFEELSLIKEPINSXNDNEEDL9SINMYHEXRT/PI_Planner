"""Bounded intent routing; data operations remain server-side."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Intent:
    name: str
    clarification: str | None = None


def metric_mentions(question: str, metric_codes: set[str]) -> list[str]:
    aliases = {
        "bus_factor": ("bus factor", "busfactor", "бас фактор", "бус фактор"),
        "pi_predictability": ("pi predictability", "предсказуемость pi", "предсказуемость пи"),
        "say_do_ratio": ("say/do", "say do", "say/do ratio", "say do ratio"),
    }
    text = question.casefold()
    return sorted(code for code in metric_codes if any(
        re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", text)
        for name in (code.casefold(), *aliases.get(code.casefold(), ()))
    ))


def classify(question: str, *, has_snapshot: bool,
             focus: tuple[str, str] | None, metric_codes: set[str],
             has_scenario: bool, has_previous_run: bool = False,
             pending_intent: str | None = None) -> Intent:
    text = question.casefold()
    if re.search(r"что изменилось|изменени[яй]|сравн.*прогон|новый прогон", text):
        return (Intent("changes") if has_snapshot and has_previous_run else
                Intent("changes", "Выберите второй прогон через обновление контекста чата.")
                if has_snapshot else Intent("changes", "Сначала выберите PI и прогон."))
    if re.search(r"сравн.*(?:мер|найм|перевод|обуч|пакет)|найм.*перевод|эффект.*(?:найм|перевод|обуч)", text):
        if not has_snapshot:
            return Intent("compare_measures", "Сначала выберите PI и прогон для расчёта мер.")
        if not has_scenario:
            return Intent("compare_measures", "Задайте меры с ролями, ставками, спринтами и навыками; "
                          "сравнение рассчитывается через сценарный API этого чата.")
        return Intent("compare_measures")
    if (re.search(r"наня[тьл]|найм|перевест[иь]|перевод|обучить|обучени", text)
            or (pending_intent == "compare_measures" and
                re.search(r"ставк|спринт|половин|четверт|сотрудник|инженер|роль|команд", text))):
        return Intent("compare_measures", "Укажите ID сотрудников, команду, роль, ставку, "
                      "спринт и навыки для мер; расчёт выполняется через сценарный API чата.")
    if (re.search(r"увелич\w*.*(?:количеств|задач)|больше.*задач|проанализируй.*что.*делать|"
                  r"что.*(?:мне|нам).*делать|какие.*предложени|с чего.*(?:начать|приступ|присту)", text)
            and not re.search(r"загруз|excel|интерфейс|как.*систем", text)):
        return (Intent("planning_actions") if has_snapshot else
                Intent("planning_actions", "Выберите PI и прогон: разберу причины переноса и с чего начать."))
    own = re.search(r"мо[яеию]|мои|именно|у нас|наш|эт[аоуи]т? (?:задач|команд|прогон|план)", text)
    if re.search(r"как (?:работает|устроен|строится|считается|рассчитыва|формируется|определяется|"
                 r"загрузить|начать|система)|что такое|что означа|что значит|чем отличается|откуда бер|"
                 r"правил[оа]|зачем|что (?:ты )?умеешь", text) and not own:
        return Intent("system_help")
    mentioned = metric_mentions(question, metric_codes)
    if len(mentioned) == 1:
        return (Intent("metric_explanation") if has_snapshot else
                Intent("metric_explanation", "Для значения KPI выберите PI и прогон; правило могу объяснить без них."))
    if (re.search(r"за[её]мн?\w*|за[её]м\b", text)
            and re.search(r"сколько|количеств|сумм", text)
            and not re.search(r"процент|дол[яю]|почему", text)):
        return (Intent("loan_hours") if has_snapshot else
                Intent("loan_hours", "Выберите PI и прогон, чтобы посчитать заёмные часы."))
    if focus:
        return Intent("task_explanation" if focus[0] == "task" else "team_analysis")
    if re.search(r"мо[яию]|мои|у нас|команд|задач|план|дефицит|прогон", text):
        if not has_snapshot and not own:
            # Чат «о системе»: общий вопрос со словом «план» — это справка, а не разбор конкретного прогона.
            return Intent("system_help")
        return (Intent("overview") if has_snapshot else
                Intent("overview", "Для разбора вашего плана выберите PI и прогон."))
    # В чате с прогоном неопознанный вопрос («что произошло?») — о прогоне: модель получает его обзор.
    return Intent("overview") if has_snapshot else Intent("system_help")


def direct_task_reason(question: str) -> bool:
    return bool(re.search(r"почему|причин|объясни.*(?:решени|перенос)|не (?:попал|вош|включен)", question.casefold()))


def operation(intent: Intent) -> str:
    common = ("Ответь на вопрос по переданным данным и истории. Не выдумывай числа или проверенный "
              "эффект мер. В summary/explanation не пиши чисел: для численного вывода верни "
              "fact_refs как массив {evidence_id, field}, где field — точный путь к числу "
              "в переданном JSON факта; сервер подставит значение. "
              "Идентификаторы объектов выводи как [entity:точный_ID] из фактов; сервер проверит их. "
              "При неоднозначности верни needs_clarification. "
              "Сначала дай прямой ответ; используй имеющиеся факты вместо запроса уже переданных данных. "
              "Не проси пользователя назвать JSON-путь или внутреннюю структуру сервера. "
              "Верни JSON с status, summary, explanation, clarification, fact_refs.")
    details = {
        "system_help": "Объясни действующие правила и порядок работы по документации; прогон не обязателен.",
        "planning_actions": "Предложи порядок устранения ограничений по сохранённому плану без выдуманного эффекта мер.",
        "loan_hours": "Покажи заёмные часы по назначениям выбранного прогона.",
        "overview": "Опиши основные ограничения и масштаб плана по полному обзору снимка.",
        "team_analysis": "Объясни положение выбранной команды и последствия для неё.",
        "task_explanation": "Разбери решение и цепочку причин выбранной задачи.",
        "metric_explanation": "Отдели факт от прогноза, укажи период и знаменатель из снимка и правила.",
        "compare_measures": "Сравни только сохранённые сценарные результаты; не складывай эффекты мер.",
        "changes": "Отдели наблюдаемую разницу прогонов от предполагаемой причины.",
    }
    return details[intent.name] + " " + common
