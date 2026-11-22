import {ApiError} from '../api';
// ARM2 platform file preview. The legacy HTML entry is retained for open tabs.
export function isFilePreviewPath(path: string): boolean {
  return /^\/arm2\/(?:questionnaires\/[A-Za-z0-9-]+\/files\/[A-Za-z0-9-]+|(?:cp|veh|cprep|org)\/[A-Za-z0-9-]+\/documents\/[A-Za-z0-9-]+\/(?:revisions\/[A-Za-z0-9-]+\/files\/[A-Za-z0-9-]+|stages\/[A-Za-z0-9-]+))\/preview$/.test(path);
}

export function openFilePreview(path?: string, name = 'Документ') {
  const url = (source: string, title: string) => {
    if (!isFilePreviewPath(source)) throw new ApiError(422,'Некорректная ссылка на файл');
    return '/questionnaire-preview.html#' + new URLSearchParams({path: source, name: title});
  };
  const destination = path ? url(path, name) : 'about:blank';
  const tab = window.open(destination, '_blank');
  if (!tab) throw new ApiError(422,'Разрешите открытие новой вкладки для просмотра вложения');
  tab.opener = null;
  return {
    navigate(source: string, title: string) {
      if (!tab.closed) tab.location.replace(url(source, title));
    },
    close() {tab.close();},
  };
}
