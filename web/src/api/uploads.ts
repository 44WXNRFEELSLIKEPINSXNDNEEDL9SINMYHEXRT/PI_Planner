/**
 * Три маршрута записи (docs/SCHEMA.md §4). Тело запроса — сам файл,
 * без multipart: `fetch(url, {method:'POST', body: file})`.
 */
import { postFile } from './client'
import type { ActualsUploadResult, DatasetUploadResult } from '../types/views'

/** POST /api/dataset — xlsx датасета. Стирает прежние прогоны и факт. */
export function uploadDataset(file: File): Promise<DatasetUploadResult> {
  return postFile<DatasetUploadResult>('/dataset', file)
}

/** POST /api/actuals?sprint=N — факт спринта N. Заменяет факт N и все более поздние. */
export function uploadActuals(file: File, sprintNo: number): Promise<ActualsUploadResult> {
  return postFile<ActualsUploadResult>('/actuals', file, { sprint: String(sprintNo) })
}

/** GET /api/actuals/template?sprint=N — CSV-шаблон для скачивания браузером. */
export function templateUrl(sprintNo?: number): string {
  return sprintNo ? `/api/actuals/template?sprint=${sprintNo}` : '/api/actuals/template'
}
