'use strict';
const search = document.getElementById('search');
search.addEventListener('input', () => {
  const query = search.value.toLocaleLowerCase('ru');
  document.querySelectorAll('tbody tr').forEach(row => { row.hidden = !row.textContent.toLocaleLowerCase('ru').includes(query); });
});
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
  const value = button.dataset.copy;
  const status = document.getElementById('copy-status');
  try {
    await navigator.clipboard.writeText(value);
    status.textContent = 'Путь скопирован. Ничего не запущено.';
  } catch {
    status.textContent = 'Автоматическое копирование недоступно. Выделите путь в таблице и нажмите Ctrl+C.';
  }
}));
