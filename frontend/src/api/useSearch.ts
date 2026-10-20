import { useApi } from "./api-context";
import { type SearchError, type SearchQuery, type SearchResults, searchDocuments } from "./search";
import { type Request, useRequest } from "./useDocuments";

/**
 * One page of keyword search results. `run` counts the owner's searches, so pressing Search again
 * with the same words asks the backend again. The key, query included, stays in React state.
 */
export function useSearchResults(
  query: SearchQuery,
  run: number,
): Request<SearchResults, SearchError> {
  const api = useApi();
  const key = JSON.stringify([run, query.query, [...query.types].sort(), query.page]);
  return useRequest(key, () => searchDocuments(api, query));
}
