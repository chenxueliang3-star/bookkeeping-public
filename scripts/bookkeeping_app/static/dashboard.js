    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
    const money = (cents) => (Number(cents || 0) / 100).toFixed(2);
    const increaseTypes = new Set(['收入']);
    const decreaseTypes = new Set(['支出']);
    const toneIncreaseTypes = new Set(['收入', '借入', '收款']);
    const toneDecreaseTypes = new Set(['支出', '借出', '还款']);
    let pendingAnimating = false;
    let entries = [];
    let accountBalances = [];
    let debtBalances = [];
    let pendingItems = [];
    let categoryOptions = [];
    let categoryTree = {};
    let accountOptions = [];
    let accountFilterOptions = [];
    let keywordRules = [];
    let summaryData = null;
    let periodMode = 'month';
    let cycleMode = 'quarter';
    let selectedCycleIndex = 0;
    let yearOffset = 0;
    let monthOffset = 0;
    let customStart = '';
    let customEnd = '';
    let periodInitialized = false;
    let activeDateTarget = null;
    let pickerMonth = new Date();
    let entryPage = 1;
    let entryPageSize = localStorage.getItem('bookkeeping.entryPageSize') || '50';
    let editingEntryId = null;
    let linkPickerRefundId = null;
    let activities = [];
    let expandedActivityId = null;
    let activityFormMode = null; // 'create'（顶部表单只做新建；改名走卡片行内编辑）
    let renamingActivityId = null; // 行内改名的活动 id
    let showArchived = false; // 总览归档折叠区展开状态
    let checkedEntries = new Set();
    let entryFilters = {year: '', month: '', day: '', type: '', category: '', categoryPrefix: '', account: '', transaction_object: '', tone: '', start: '', end: ''};
    let entryTypes = [];
    let pendingDelete = null;
    let pendingCategoryDelete = null;
    let pendingDebtDelete = null;
    let pendingAccountDelete = null;
    let pendingSuggestionCache = {};
    let pendingSkippedIds = JSON.parse(localStorage.getItem('bookkeeping.pendingSkippedIds') || '[]');
    let expenseCategoryLevel = localStorage.getItem('bookkeeping.expenseCategoryLevel') || 'major';
    let editingCategoryKey = null;
    let addingKeywordKey = null;
    let editingAccountName = null;
    let editingAccountBalanceName = null;
    let addingAccountType = null;      // 资产管家：当前选中的添加账户类型（8 宫格）
    let editingAssetTypeName = null;   // 资产管家：行内纠正 asset_type 的账户名
    let editingPrincipalName = null;   // 负债卡片：行内编辑本金的账户名

    async function api(path, options) {
      const requestOptions = {...(options || {}), cache: 'no-store'};
      const res = await fetch(path, requestOptions);
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || res.statusText);
      return data;
    }

    function savePendingSkippedIds() {
      localStorage.setItem('bookkeeping.pendingSkippedIds', JSON.stringify(pendingSkippedIds));
    }

    function orderPendingItems(items) {
      const itemKey = item => String(item.ui_id || item.id);
      const availableIds = new Set(items.map(itemKey));
      pendingSkippedIds = pendingSkippedIds.filter(id => availableIds.has(String(id)));
      savePendingSkippedIds();
      const skippedIndex = new Map(pendingSkippedIds.map((id, index) => [String(id), index]));
      return [...items].sort((a, b) => {
        const ai = skippedIndex.has(itemKey(a)) ? skippedIndex.get(itemKey(a)) : -1;
        const bi = skippedIndex.has(itemKey(b)) ? skippedIndex.get(itemKey(b)) : -1;
        if (ai === -1 && bi === -1) return 0;
        if (ai === -1) return -1;
        if (bi === -1) return 1;
        return ai - bi;
      });
    }

    function signedMoney(cents, tone = 'neutral') {
      const value = Math.abs(Number(cents || 0)) / 100;
      const sign = tone === 'income' || tone === 'refund' ? '+' : tone === 'expense' ? '-' : Number(cents || 0) < 0 ? '-' : '+';
      return `${sign}${value.toFixed(2)}`;
    }

    function balanceMoney(cents) {
      const value = Number(cents || 0) / 100;
      return value < 0 ? `-${Math.abs(value).toFixed(2)}` : value.toFixed(2);
    }

    function moneyTone(row) {
      if (!row) return 'neutral';
      if (row.type === '余额修正') return 'neutral';
      if (row.type === '退款') return 'refund';
      if (toneIncreaseTypes.has(row.type)) return 'income';
      if (toneDecreaseTypes.has(row.type)) return 'expense';
      return 'neutral';
    }

    function reportableEntry(row) {
      return row && row.type !== '转账' && row.type !== '余额修正' && row.category !== '转账';
    }

    // 退款净额制：退款冲减同一商户上一笔支出的分类，找不到配对则记入"退款冲减"
    function refundPairCategory(refund) {
      const obj = String(refund.transaction_object || '').trim();
      let best = null;
      for (const entry of entries) {
        if (entry.type !== '支出' || !entry.category || entry.category === '转账') continue;
        if (obj && String(entry.transaction_object || '').trim() !== obj) continue;
        if (String(entry.transaction_time) > String(refund.transaction_time)) continue;
        if (!best || String(entry.transaction_time) > String(best.transaction_time)) best = entry;
      }
      return best ? best.category : '退款冲减';
    }

    function moneyClass(_value, row) {
      const tone = moneyTone(row);
      return `money-cell ${tone === 'income' || tone === 'refund' ? 'income' : tone === 'expense' ? 'expense' : 'neutral-money'}`;
    }

    function signedEntryMoney(cents, row) {
      return signedMoney(cents, moneyTone(row));
    }

    function amountValue(cents) {
      return (Math.abs(Number(cents || 0)) / 100).toFixed(2);
    }

    function nowDatetimeLocal() {
      const date = new Date();
      date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
      return date.toISOString().slice(0, 16);
    }

    function datetimeInputValue(value) {
      return String(value || '').replace(' ', 'T').slice(0, 16);
    }

    function optionsHtml(options, selected, allowBlank = false) {
      const values = [...new Set(options.filter(value => value !== undefined && value !== null && String(value).trim() !== '').map(String))];
      if (selected && !values.includes(String(selected))) values.unshift(String(selected));
      const blank = allowBlank ? '<option value=""></option>' : '';
      return blank + values.map(value => `<option value="${esc(value)}" ${String(value) === String(selected || '') ? 'selected' : ''}>${esc(value)}</option>`).join('');
    }

    function categoryLabel(category) {
      return String(category || '').replaceAll('/', '·');
    }

    function majorCategory(category) {
      const value = String(category || '').trim();
      if (!value || value === '未分类') return '未分类';
      return value.split('/')[0] || value;
    }

    // ---------- 分类级联下拉 ----------
    // 两级级联菜单（仿 VS Code 子菜单）：主列表 = 一级大类，hover/点击展开右侧二级子菜单，
    // 点二级选中"大类/二级"。输入框打字时切换为平铺过滤模式（全文匹配，熟练用户打字更快）。
    // DOM：外层 .category-dropdown（fixed 定位，不裁剪子菜单）> .category-dropdown-list（主列表，滚动）
    //      + .category-dropdown-sub（二级子菜单，absolute，右侧空间不足自动左翻）。
    let catDropdownEl = null;       // 外层面板
    let catListEl = null;           // 主列表（滚动容器）
    let catSubEl = null;            // 二级子菜单
    let catDropdownInput = null;    // 触发浮层的输入框或锚点元素
    let catState = null;            // {tree, flat, allowMajor, mode, onPick, onExtra, extraLabel, currentValue}
    let catDropdownActive = -1;     // 平铺过滤模式高亮（-1 = 无高亮；鼠标 hover 会同步此值）
    let catMajorActive = -1;        // 级联模式高亮大类
    let catSubActive = -1;          // 子菜单高亮二级
    let catHoverTimer = null;

    function closeCategoryDropdown() {
      if (catHoverTimer) { clearTimeout(catHoverTimer); catHoverTimer = null; }
      if (catDropdownEl) catDropdownEl.style.display = 'none';
      if (catSubEl) catSubEl.style.display = 'none';
      catDropdownInput = null;
      catState = null;
      catDropdownActive = -1;
      catMajorActive = -1;
      catSubActive = -1;
    }

    function closeSubEl() {
      if (catSubEl) catSubEl.style.display = 'none';
      catSubActive = -1;
    }

    // 浮层底部固定操作项（快速记账的"+ 新增分类"、筛选的"清除分类筛选"）
    function extraItemsHtml() {
      const parts = [];
      if (catState.onExtra) {
        parts.push(`<button type="button" class="category-dropdown-item category-dropdown-extra" data-cat-extra="1">${esc(catState.extraLabel || '+ 新增分类')}</button>`);
      }
      if (catState.allowMajor && (entryFilters.category || entryFilters.categoryPrefix)) {
        parts.push('<button type="button" class="category-dropdown-item category-dropdown-extra" data-cat-clear="1">清除分类筛选</button>');
      }
      return parts.join('');
    }

    function renderCascadeMajors() {
      if (!catState) return;
      const majors = Object.keys(catState.tree);
      catListEl.innerHTML = majors.map((major, i) => {
        const hasSub = (catState.tree[major] || []).length > 0;
        return `<button type="button" class="category-dropdown-item${i === catMajorActive ? ' active' : ''}" data-major="${esc(major)}" data-index="${i}">
          <span>${esc(major)}</span>${hasSub ? '<span class="category-sub-arrow">▸</span>' : ''}
        </button>`;
      }).join('') + extraItemsHtml();
    }

    function highlightSub() {
      if (!catSubEl) return;
      [...catSubEl.children].forEach((btn, i) => btn.classList.toggle('active', i === catSubActive));
      const active = catSubEl.children[catSubActive];
      if (active) active.scrollIntoView({block: 'nearest'});
    }

    // 子菜单跟随主列表里 active 大类的位置（列表滚动时也要重算）
    function positionCascadeSub() {
      if (!catState || !catDropdownEl || !catSubEl || catSubEl.style.display === 'none' || catMajorActive < 0) return;
      const btn = catListEl.querySelectorAll('button[data-major]')[catMajorActive];
      if (!btn) return;
      const panelRect = catDropdownEl.getBoundingClientRect();
      const subH = Math.min(catSubEl.offsetHeight, 260);
      const top = Math.max(0, Math.min(catListEl.offsetTop + btn.offsetTop - catListEl.scrollTop, panelRect.height - subH - 4));
      catSubEl.style.top = `${Math.round(top)}px`;
    }

    function openCascadeSub(index) {
      if (!catState || !catDropdownEl) return;
      const majors = Object.keys(catState.tree);
      const major = majors[index];
      if (major === undefined) return;
      const leaves = catState.tree[major] || [];
      if (!leaves.length) return;
      catMajorActive = index;
      renderCascadeMajors();
      const items = [];
      // 筛选场景：子菜单顶部提供"整个大类"项 = 按大类前缀筛选
      if (catState.allowMajor) {
        items.push(`<button type="button" class="category-dropdown-item" data-major-only="${esc(major)}">整个「${esc(major)}」全部</button>`);
      }
      items.push(...leaves.map(leaf =>
        `<button type="button" class="category-dropdown-item" data-leaf="${esc(categoryPath(major, leaf))}">${esc(leaf)}</button>`));
      catSubEl.innerHTML = items.join('');
      catSubEl.style.display = 'block';
      // 高亮当前值所属二级
      const current = String(catState.currentValue || '');
      const leafIdx = leaves.findIndex(leaf => categoryPath(major, leaf) === current);
      catSubActive = leafIdx >= 0 ? leafIdx + (catState.allowMajor ? 1 : 0) : -1;
      // 水平翻转：右侧放不下就翻到左侧
      const panelRect = catDropdownEl.getBoundingClientRect();
      const subWidth = Math.max(catSubEl.offsetWidth, 160);
      catSubEl.classList.toggle('flip-left', window.innerWidth - panelRect.right < subWidth + 8);
      positionCascadeSub();
      highlightSub();
    }

    function renderFlatFilter(filter) {
      if (!catState) return;
      const all = catState.flat;
      const q = String(filter || '').trim();
      const list = q ? all.filter(value => value.includes(q) || categoryLabel(value).includes(q)) : all;
      catSubEl.style.display = 'none';
      // -1（无高亮）保持；>=0 才收敛到有效范围
      if (catDropdownActive >= 0) catDropdownActive = Math.min(catDropdownActive, list.length - 1);
      let html = list.map((value, i) =>
        `<button type="button" class="category-dropdown-item${i === catDropdownActive ? ' active' : ''}" data-leaf="${esc(value)}">${esc(categoryLabel(value))}</button>`
      ).join('');
      // 一级选择模式：输入了列表外的新名字 → 底部出现「＋ 新增」项，把"可直接输入"变成看得见
      if (catState.majorsOnly && q && !all.includes(q)) {
        html += `<button type="button" class="category-dropdown-item category-dropdown-extra" data-cat-new="${esc(q)}">＋ 新增「${esc(q)}」</button>`;
      }
      if (!html) {
        catListEl.innerHTML = '<div class="category-dropdown-empty">无匹配分类（可直接输入自定义）</div>';
        catDropdownEl.dataset.count = '0';
        return;
      }
      catListEl.innerHTML = html + extraItemsHtml();
      catDropdownEl.dataset.count = String(list.length);
    }

    function pickCategory(value, meta = {}) {
      const anchor = catDropdownInput;
      const state = catState;
      closeCategoryDropdown();
      if (!anchor || !state) return;
      if (state.onPick) { state.onPick(value, meta); return; }
      anchor.value = value;
      anchor.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function pickFromBtn(btn) {
      if (btn.dataset.leaf !== undefined) { pickCategory(btn.dataset.leaf); return; }
      if (btn.dataset.catNew !== undefined) { pickCategory(btn.dataset.catNew); return; }
      if (btn.dataset.majorOnly !== undefined) { pickCategory(btn.dataset.majorOnly, {majorOnly: true}); return; }
      if (btn.dataset.major !== undefined) pickCategory(btn.dataset.major);
    }

    // 筛选场景没有 type 限制时，把所有类型的分类树合并（同大类去重合并二级）
    function mergedCategoryTree() {
      const merged = {};
      for (const type of entryTypes) {
        for (const [major, leaves] of Object.entries(categoryTree[type] || {})) {
          merged[major] = [...new Set([...(merged[major] || []), ...leaves])];
        }
      }
      return merged;
    }

    // 浮层垂直定位：默认向下展开；下方放不下且上方够用时向上翻；都放不下贴顶（内部滚动）
    function positionCategoryDropdown() {
      if (!catDropdownEl || !catDropdownInput || catDropdownEl.style.display === 'none') return;
      const rect = catDropdownInput.getBoundingClientRect();
      const h = catDropdownEl.offsetHeight || 264;
      let top = rect.bottom + 4;
      if (top + h > window.innerHeight - 8 && rect.top - h - 8 > 8) top = rect.top - h - 4;
      if (top < 8) top = 8;
      catDropdownEl.style.top = `${Math.round(top)}px`;
    }

    function openCategoryDropdown(anchor) {
      buildCategoryPanel();
      catDropdownInput = anchor;
      const isFilter = anchor.dataset.catFilter === '1';
      const type = anchor.dataset.catType || '';
      const tree = type ? (categoryTree[type] || {}) : mergedCategoryTree();
      // 一级选择模式（data-cat-majors）：平铺列出大类名，点选即填，仍可自由键入新名
      const majorsOnly = anchor.dataset.catMajors === '1';
      const flat = majorsOnly ? Object.keys(tree) : (type ? categoriesForType(type) : entryFilterCategoryOptions());
      catState = {
        tree,
        flat,
        allowMajor: isFilter,
        mode: majorsOnly ? 'flat' : 'cascade',
        majorsOnly,
        currentValue: isFilter
          ? (entryFilters.categoryPrefix || entryFilters.category || '')
          : String(anchor.value || '').trim(),
        onPick: isFilter ? applyCategoryHeaderFilter : null,
        onExtra: anchor.dataset.catExtra === '1' ? promptAddQuickCategory : null,
        extraLabel: anchor.dataset.catExtra === '1' ? '+ 新增分类' : ''
      };
      const rect = anchor.getBoundingClientRect();
      catDropdownEl.style.left = `${Math.round(rect.left)}px`;
      catDropdownEl.style.top = `${Math.round(rect.bottom + 4)}px`;
      catDropdownEl.style.minWidth = `${Math.max(Math.round(rect.width), 170)}px`;
      catDropdownEl.style.display = 'block';
      // 垂直翻转：下方空间放不下浮层时向上展开（放在内容渲染后再调用 positionDropdown 精确定位）
      positionCategoryDropdown();
      if (anchor instanceof HTMLInputElement) {
        // 全选旧值（直接打字即替换，不必手动删除）。必须延迟：mousedown 的浏览器
        // 默认动作（定位光标、清除选区）会在本 handler 同步代码之后执行并覆盖 select()
        setTimeout(() => { if (catDropdownInput === anchor) anchor.select(); }, 0);
      }
      if (majorsOnly) {
        // 无当前值时不预高亮任何项（避免"餐饮永远高亮"）
        catDropdownActive = flat.indexOf(catState.currentValue);
        renderFlatFilter('');
        positionCategoryDropdown();
        return;
      }
      // 级联模式：当前值定位展开对应大类；无当前值则只列大类
      const majors = Object.keys(tree);
      if (!majors.length) { catState.mode = 'flat'; catDropdownActive = 0; renderFlatFilter(''); positionCategoryDropdown(); return; }
      catMajorActive = majors.indexOf(String(catState.currentValue).split('/')[0]);
      if (catMajorActive >= 0 && (tree[majors[catMajorActive]] || []).length) {
        renderCascadeMajors();
        openCascadeSub(catMajorActive);
      } else {
        closeSubEl();
        renderCascadeMajors();
      }
      positionCategoryDropdown();
    }

    function buildCategoryPanel() {
      if (catDropdownEl) return;
      catDropdownEl = document.createElement('div');
      catDropdownEl.className = 'category-dropdown';
      catDropdownEl.style.display = 'none';
      catListEl = document.createElement('div');
      catListEl.className = 'category-dropdown-list';
      catSubEl = document.createElement('div');
      catSubEl.className = 'category-dropdown-sub';
      catSubEl.style.display = 'none';
      catDropdownEl.append(catListEl, catSubEl);
      document.body.appendChild(catDropdownEl);
      // mousedown 先于 blur：preventDefault 让输入框保持焦点
      catDropdownEl.addEventListener('mousedown', event => {
        event.preventDefault();
        const btn = event.target.closest('button');
        if (!btn) return;
        if (btn.dataset.catExtra) { const fn = catState && catState.onExtra; closeCategoryDropdown(); if (fn) fn(); return; }
        if (btn.dataset.catClear) { closeCategoryDropdown(); clearHeaderCategoryFilter(); return; }
        pickFromBtn(btn);
      });
      catListEl.addEventListener('mouseover', event => {
        if (!catState) return;
        // 平铺模式：鼠标 hover 同步键盘高亮（始终只有一个高亮项）
        if (catState.mode === 'flat') {
          const btn = event.target.closest('button.category-dropdown-item');
          if (!btn) return;
          const items = [...catListEl.querySelectorAll('.category-dropdown-item')];
          const idx = items.indexOf(btn);
          if (idx >= 0 && idx !== catDropdownActive) {
            catDropdownActive = idx;
            items.forEach((b, i) => b.classList.toggle('active', i === idx));
          }
          return;
        }
        const btn = event.target.closest('button[data-major]');
        if (!btn) return;
        const idx = Number(btn.dataset.index);
        if (idx === catMajorActive) return;
        clearTimeout(catHoverTimer);
        catHoverTimer = setTimeout(() => {
          if (!catState) return;
          if ((catState.tree[btn.dataset.major] || []).length) openCascadeSub(idx);
          else { catMajorActive = idx; closeSubEl(); renderCascadeMajors(); }
        }, 60);
      });
      catListEl.addEventListener('scroll', positionCascadeSub);
    }

    function bindCategoryDropdownDelegates() {
      document.addEventListener('mousedown', event => {
        const input = event.target.closest('.cat-search');
        if (input) {
          // 再次点击输入框本身 = 收起；否则打开
          if (catDropdownInput === input && catDropdownEl && catDropdownEl.style.display !== 'none') closeCategoryDropdown();
          else openCategoryDropdown(input);
          return;
        }
        const trigger = event.target.closest('.cat-filter-trigger');
        if (trigger) {
          if (catDropdownInput === trigger && catDropdownEl && catDropdownEl.style.display !== 'none') closeCategoryDropdown();
          else openCategoryDropdown(trigger);
          return;
        }
        if (!event.target.closest('.category-dropdown')) closeCategoryDropdown();
      });
      document.addEventListener('input', event => {
        if (catDropdownInput && event.target === catDropdownInput && catState) {
          const q = event.target.value.trim();
          if (q) {
            // 平铺过滤模式：全文匹配"大类/二级"
            catState.mode = 'flat';
            catDropdownActive = Math.max(0, catState.flat.indexOf(q));
            renderFlatFilter(q);
          } else {
            catState.mode = 'cascade';
            catMajorActive = -1;
            closeSubEl();
            renderCascadeMajors();
          }
        }
      });
      document.addEventListener('keydown', event => {
        if (event.target.classList?.contains('cat-search') && (!catDropdownInput || !catDropdownEl || catDropdownEl.style.display === 'none') && event.key.length === 1) {
          openCategoryDropdown(event.target); // 键盘直接输入也能唤起
          return;
        }
        if (!catDropdownInput || !catDropdownEl || catDropdownEl.style.display === 'none') return;
        if (event.key === 'Escape') {
          event.stopPropagation();
          closeCategoryDropdown();
          return;
        }
        if (catState.mode === 'flat') {
          const count = Number(catDropdownEl.dataset.count || 0);
          if (event.key === 'ArrowDown' && count > 0) {
            event.preventDefault();
            catDropdownActive = Math.min(catDropdownActive + 1, count - 1);
            renderFlatFilter(catDropdownInput.value);
          } else if (event.key === 'ArrowUp' && count > 0) {
            event.preventDefault();
            catDropdownActive = catDropdownActive < 0 ? count - 1 : Math.max(catDropdownActive - 1, 0);
            renderFlatFilter(catDropdownInput.value);
          } else if (event.key === 'Enter') {
            const btn = catListEl.querySelectorAll('.category-dropdown-item')[catDropdownActive];
            if (btn) {
              event.preventDefault();
              pickFromBtn(btn);
            } else if (catState.majorsOnly && catDropdownInput.value.trim()) {
              // 无高亮时 Enter = 确认输入的新一级名称
              event.preventDefault();
              pickCategory(catDropdownInput.value.trim());
            }
          }
          return;
        }
        // 级联模式键盘导航：↑↓ 移动，→ 进子菜单，← 退回，Enter 选中
        const majors = Object.keys(catState.tree);
        const inSub = catSubEl.style.display !== 'none' && catSubActive >= 0;
        const moveMajor = next => {
          catMajorActive = next;
          if ((catState.tree[majors[catMajorActive]] || []).length) openCascadeSub(catMajorActive);
          else { closeSubEl(); renderCascadeMajors(); }
        };
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          if (inSub) { catSubActive = Math.min(catSubActive + 1, catSubEl.children.length - 1); highlightSub(); }
          else if (catMajorActive < majors.length - 1) moveMajor(catMajorActive + 1);
          else if (catMajorActive < 0 && majors.length) moveMajor(0);
        } else if (event.key === 'ArrowUp') {
          event.preventDefault();
          if (inSub) {
            if (catSubActive === 0) closeSubEl(); // 退回一级
            else { catSubActive -= 1; highlightSub(); }
          } else if (catMajorActive > 0) moveMajor(catMajorActive - 1);
        } else if (event.key === 'ArrowRight') {
          if (!inSub && catMajorActive >= 0 && (catState.tree[majors[catMajorActive]] || []).length) {
            event.preventDefault();
            openCascadeSub(catMajorActive);
            catSubActive = 0;
            highlightSub();
          }
        } else if (event.key === 'ArrowLeft') {
          if (inSub) { event.preventDefault(); closeSubEl(); }
        } else if (event.key === 'Enter') {
          event.preventDefault();
          if (inSub) {
            const btn = catSubEl.children[catSubActive];
            if (btn) pickFromBtn(btn);
          } else if (catMajorActive >= 0) {
            const major = majors[catMajorActive];
            if ((catState.tree[major] || []).length) {
              openCascadeSub(catMajorActive);
              catSubActive = 0;
              highlightSub();
            } else pickCategory(major);
          }
        }
      });
      // 浮层是 fixed 定位，页面滚动/缩放会错位，直接收起（浮层内部滚动除外）
      window.addEventListener('scroll', event => {
        if (catDropdownEl && !catDropdownEl.contains(event.target)) closeCategoryDropdown();
      }, true);
      window.addEventListener('resize', closeCategoryDropdown);
    }

    bindCategoryDropdownDelegates();

    function uniqueEntryValues(field) {
      return [...new Set(entries.map(entry => String(entry[field] || '').trim()).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'));
    }

    function entryFilterCategoryOptions() {
      return entryFilters.type ? categoriesForType(entryFilters.type) : categoryOptions;
    }

    function filterValueLabel(field) {
      if (field === 'category') return categoryLabel(entryFilters.category);
      return entryFilters[field] || '';
    }

    function entryDateParts() {
      const years = [...new Set(entries.map(entry => entryAnchorDay(entry).slice(0, 4)).filter(Boolean))].sort().reverse();
      const months = Array.from({length: 12}, (_, i) => pad2(i + 1));
      const maxDay = entryFilters.year && entryFilters.month ? new Date(Number(entryFilters.year), Number(entryFilters.month), 0).getDate() : 31;
      const days = Array.from({length: maxDay}, (_, i) => pad2(i + 1));
      return {years, months, days};
    }

    function syncEntryDateRange() {
      const selectedDay = String(entryFilters.day || '').includes('-') ? String(entryFilters.day).slice(8, 10) : entryFilters.day;
      entryFilters.start = '';
      entryFilters.end = '';
      if (!entryFilters.year) {
        entryFilters.month = '';
        entryFilters.day = '';
        return;
      }
      if (!entryFilters.month) {
        entryFilters.start = `${entryFilters.year}-01-01`;
        entryFilters.end = `${entryFilters.year}-12-31`;
        return;
      }
      const lastDay = pad2(new Date(Number(entryFilters.year), Number(entryFilters.month), 0).getDate());
      if (!selectedDay) {
        entryFilters.day = '';
        entryFilters.start = `${entryFilters.year}-${entryFilters.month}-01`;
        entryFilters.end = `${entryFilters.year}-${entryFilters.month}-${lastDay}`;
        return;
      }
      entryFilters.day = `${entryFilters.year}-${entryFilters.month}-${selectedDay}`;
    }

    function clearEntryFilters() {
      entryFilters = {year: '', month: '', day: '', type: '', category: '', categoryPrefix: '', account: '', transaction_object: '', tone: '', start: '', end: ''};
    }

    function headerFilterCell(field, label, options, attrs = '') {
      // 分类列特殊：用级联菜单（一级=按大类前缀筛选，二级=精确筛选），不再用原生 select
      if (field === 'category') return headerCategoryFilterCell(label);
      const html = optionsHtml(options, entryFilters[field], true);
      const value = filterValueLabel(field);
      return `
        <th>
          <div class="entry-head-filter ${value ? 'is-active' : ''}">
            <span class="entry-head-label">${esc(label)}<span class="entry-head-arrow">⌄</span></span>
            ${value ? `<span class="entry-head-value">${esc(value)}</span>` : ''}
            <select data-entry-filter="${field}" aria-label="${esc(label)}" ${attrs}>${html}</select>
          </div>
        </th>`;
    }

    function headerCategoryFilterCell(label) {
      const value = entryFilters.categoryPrefix
        ? `${categoryLabel(entryFilters.categoryPrefix)}（全部）`
        : categoryLabel(entryFilters.category);
      return `
        <th>
          <div class="entry-head-filter cat-filter-trigger ${value ? 'is-active' : ''}" data-cat-anchor="category" data-cat-filter="1" title="点击选择分类筛选">
            <span class="entry-head-label">${esc(label)}<span class="entry-head-arrow">⌄</span></span>
            ${value ? `<span class="entry-head-value">${esc(value)}</span>` : ''}
          </div>
        </th>`;
    }

    // 表头分类筛选选中回调：一级大类 = 前缀筛选（含该大类全部二级），二级叶子 = 精确筛选
    function applyCategoryHeaderFilter(value, meta = {}) {
      if (meta && meta.majorOnly) {
        entryFilters.categoryPrefix = value;
        entryFilters.category = '';
      } else {
        entryFilters.category = value;
        entryFilters.categoryPrefix = '';
      }
      entryPage = 1;
      renderEntries();
    }

    function clearHeaderCategoryFilter() {
      entryFilters.category = '';
      entryFilters.categoryPrefix = '';
      entryPage = 1;
      renderEntries();
    }

    function dateHeaderCell() {
      const {years, months, days} = entryDateParts();
      const dayPart = String(entryFilters.day || '').includes('-') ? String(entryFilters.day).slice(8, 10) : entryFilters.day;
      const dateText = entryFilters.year
        ? [entryFilters.year, entryFilters.month, dayPart].filter(Boolean).join('/')
        : '';
      return `
        <th>
          <div class="entry-head-filter entry-time-head ${dateText ? 'is-active' : ''}">
            <span class="entry-head-label">时间<span class="entry-head-arrow">⌄</span></span>
            ${dateText ? `<span class="entry-head-value">${esc(dateText)}</span>` : ''}
            <div class="entry-time-selects">
              <select data-entry-filter="year" aria-label="年">${optionsHtml(years, entryFilters.year, true)}</select>
              <select data-entry-filter="month" aria-label="月" ${entryFilters.year ? '' : 'disabled'}>${optionsHtml(months, entryFilters.month, true)}</select>
              <select data-entry-filter="day" aria-label="日" ${entryFilters.year && entryFilters.month ? '' : 'disabled'}>${optionsHtml(days, dayPart, true)}</select>
            </div>
          </div>
        </th>`;
    }

    // 退款关联映射：expense_id -> [退款...]（每次数据刷新后重建）
    let entryRefundLinks = new Map();
    let entriesById = new Map();
    function rebuildRefundLinks() {
      entryRefundLinks = new Map();
      entriesById = new Map(entries.map(e => [Number(e.id), e]));
      entries.forEach(e => {
        if (e.type === '退款' && e.refund_pair_id) {
          const key = Number(e.refund_pair_id);
          const list = entryRefundLinks.get(key) || [];
          list.push(e);
          entryRefundLinks.set(key, list);
        }
      });
    }
    // 已关联退款的时间归属：原支出发生那天（净额口径：退费扣在实际支出那天，跨日期也归位）
    function entryAnchorTime(entry) {
      if (entry && entry.type === '退款' && entry.refund_pair_id) {
        const src = entriesById.get(Number(entry.refund_pair_id));
        if (src) return String(src.transaction_time || '');
      }
      return String(entry?.transaction_time || '');
    }
    const entryAnchorDay = entry => entryAnchorTime(entry).slice(0, 10);
    function linkedRefundsOf(expense) {
      return expense && expense.type === '支出' ? (entryRefundLinks.get(Number(expense.id)) || []) : [];
    }
    function netAfterRefunds(expense) {
      return Number(expense.amount_cents || 0) - linkedRefundsOf(expense).reduce((sum, r) => sum + Number(r.amount_cents || 0), 0);
    }

    function entryMatchesFilters(entry) {
      const day = entryAnchorDay(entry);
      if (entryFilters.day && day !== entryFilters.day) return false;
      if (entryFilters.start && day < entryFilters.start) return false;
      if (entryFilters.end && day > entryFilters.end) return false;
      if (entryFilters.type && entry.type !== entryFilters.type) return false;
      if (entryFilters.category && (entry.category || '') !== entryFilters.category) return false;
      if (entryFilters.categoryPrefix) {
        const cat = entry.category || '';
        if (cat !== entryFilters.categoryPrefix && !cat.startsWith(entryFilters.categoryPrefix + '/')) return false;
      }
      if (entryFilters.account && (entry.account || '') !== entryFilters.account && (entry.target_account || '') !== entryFilters.account) return false;
      if (entryFilters.bill_source) {
        // 来源筛选：'(无来源)' 特殊值筛出 bill_source 为空的账单（便于找出待补充来源的）
        if (entryFilters.bill_source === '(无来源)') {
          if ((entry.bill_source || '').trim()) return false;
        } else if ((entry.bill_source || '').trim() !== entryFilters.bill_source) return false;
      }
      if (entryFilters.transaction_object && (entry.transaction_object || '') !== entryFilters.transaction_object) return false;
      if (entryFilters.tone) {
        if (entryFilters.tone === 'refund') {
          // 退款视图：显示全部退款 + 已被关联的原支出（成对呈现）
          const isRefund = entry.type === '退款';
          const hasLinks = linkedRefundsOf(entry).length > 0;
          if (!isRefund && !hasLinks) return false;
        } else if (moneyTone(entry) !== entryFilters.tone) {
          return false;
        }
      }
      return true;
    }

    function entryFilterSummary() {
      const parts = [];
      if (entryFilters.tone === 'income') parts.push('收入账单');
      if (entryFilters.tone === 'expense') parts.push('支出账单');
      if (entryFilters.tone === 'refund') parts.push('退款配对');
      if (entryFilters.start || entryFilters.end) parts.push(`${slashDate(entryFilters.start)} - ${slashDate(entryFilters.end)}`);
      if (entryFilters.day) parts.push(slashDate(entryFilters.day));
      ['type', 'category', 'categoryPrefix', 'bill_source', 'account', 'transaction_object'].forEach(field => {
        if (entryFilters[field]) {
          if (field === 'category' || field === 'categoryPrefix') parts.push(categoryLabel(entryFilters[field]));
          else parts.push(entryFilters[field]);
        }
      });
      return parts.join(' / ');
    }

    function plainMoney(cents) {
      const v = Number(cents || 0) / 100;
      return (v < 0 ? '-' : '') + Math.abs(v).toFixed(2);
    }
    // 退款行的"类型"列：附加 关联/未关联 徽章
    function typeCellWithRefund(value, row) {
      if (row.type !== '退款') return value;
      const linked = Boolean(row.refund_pair_id);
      return {
        text: `${value}（${linked ? '已关联' : '未关联'}）`,
        // cell 容器是 -webkit-box 竖排（两行截断用），文字与徽章各自独立成行：
        // 第一行类型文字，第二行徽章，避免横向挤压截断
        html: `<span class="cell-nowrap">${esc(value)}</span><span class="cell-nowrap refund-badge ${linked ? 'ok' : 'warn'}">${linked ? '已关联' : '未关联'}</span>`,
      };
    }
    // 原支出的"金额"列：已关联退款时附加净额徽章
    function amountCellWithNet(value, row) {
      const shown = signedEntryMoney(value, row);
      if (row.type !== '支出') return shown;
      const links = linkedRefundsOf(row);
      if (!links.length) return shown;
      const net = netAfterRefunds(row);
      const refundDesc = links.map(r => plainMoney(r.amount_cents)).join('、');
      return {
        text: shown,
        // 同 typeCellWithRefund：金额一行，净额徽章第二行，避免挤压截断
        html: `<span class="cell-nowrap">${esc(shown)}</span><span class="cell-nowrap refund-net" title="已关联退款 ${refundDesc} 元，净支出 ${plainMoney(net)} 元">净 ${plainMoney(net)}</span>`,
      };
    }

    function clearDeleteToast() {
      const toast = $('deleteToast');
      if (toast) toast.remove();
    }

    function renderDeleteToast() {
      clearDeleteToast();
      const pending = pendingDelete || pendingCategoryDelete || pendingDebtDelete || pendingAccountDelete;
      if (!pending) return;
      const toast = document.createElement('div');
      toast.id = 'deleteToast';
      toast.className = 'delete-toast';
      const labels = {
        account: `已移除账户 ${pending.row.name}`,
        category: `已移除分类 ${categoryLabel(pending.category)}`,
        debt: `已移除 ${pending.row.name}`,
        entry: '已移除 1 条账单',
      };
      const label = labels[pending.kind || 'entry'];
      toast.innerHTML = `
        <span>${esc(label)}</span>
        <button data-undo-delete>撤销(${pending.remaining}s)</button>`;
      document.body.appendChild(toast);
      toast.querySelector('[data-undo-delete]').onclick = pending.kind === 'category'
        ? undoPendingCategoryDelete
        : pending.kind === 'debt'
          ? undoPendingDebtDelete
          : pending.kind === 'account'
            ? undoPendingAccountDelete
            : undoPendingDelete;
    }

    function pendingDeleteRequest(item) {
      if ((item.kind || 'entry') === 'category') {
        return {path: '/api/category-delete', body: {type: item.type, category: item.category}};
      }
      if (item.kind === 'debt') {
        return {path: '/api/account-debt-delete', body: {id: item.row.id}};
      }
      if (item.kind === 'account') {
        return {path: '/api/account-delete', body: {name: item.row.name}};
      }
      return {path: '/api/entry-delete', body: {id: item.row.id}};
    }

    function sendPendingDelete(item, keepalive = false) {
      const request = pendingDeleteRequest(item);
      const body = JSON.stringify(request.body);
      if (keepalive) {
        const blob = new Blob([body], {type: 'application/json'});
        if (navigator.sendBeacon && navigator.sendBeacon(request.path, blob)) return Promise.resolve();
        return fetch(request.path, {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body,
          keepalive: true
        }).catch(() => {});
      }
      return api(request.path, {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body
      });
    }

    function takePendingDeletes() {
      const items = [pendingDelete, pendingCategoryDelete, pendingDebtDelete, pendingAccountDelete].filter(Boolean);
      pendingDelete = null;
      pendingCategoryDelete = null;
      pendingDebtDelete = null;
      pendingAccountDelete = null;
      items.forEach(item => {
        clearInterval(item.intervalId);
        clearTimeout(item.timeoutId);
      });
      clearDeleteToast();
      return items;
    }

    async function commitPendingDeletes(options = {}) {
      const items = takePendingDeletes();
      if (!items.length) return;
      try {
        await Promise.all(items.map(item => sendPendingDelete(item, Boolean(options.keepalive))));
        if (options.refreshAfter) await refresh();
      } catch (err) {
        if (options.refreshAfter) await refresh();
        if (!options.silent) alert(err.message === 'not found' ? '后端接口未加载，请重启 Dashboard 后再删除。' : err.message);
      }
    }

    async function finalizePendingDelete() {
      if (!pendingDelete) return;
      const item = pendingDelete;
      pendingDelete = null;
      clearInterval(item.intervalId);
      clearTimeout(item.timeoutId);
      clearDeleteToast();
      try {
        await api('/api/entry-delete', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({id: item.row.id})
        });
        await refresh();
      } catch (err) {
        if (!entries.some(entry => Number(entry.id) === Number(item.row.id))) {
          entries.splice(Math.min(item.index, entries.length), 0, item.row);
        }
        renderEntries();
        alert(err.message === 'not found' ? '后端接口未加载，请重启 Dashboard 后再删除。' : err.message);
      }
    }

    function undoPendingDelete() {
      if (!pendingDelete) return;
      const item = pendingDelete;
      pendingDelete = null;
      clearInterval(item.intervalId);
      clearTimeout(item.timeoutId);
      clearDeleteToast();
      if (!entries.some(entry => Number(entry.id) === Number(item.row.id))) {
        entries.splice(Math.min(item.index, entries.length), 0, item.row);
      }
      renderEntries();
    }

    function scheduleEntryDelete(id) {
      if (pendingCategoryDelete) finalizePendingCategoryDelete();
      if (pendingDebtDelete) finalizePendingDebtDelete();
      if (pendingAccountDelete) finalizePendingAccountDelete();
      if (pendingDelete) finalizePendingDelete();
      const index = entries.findIndex(entry => Number(entry.id) === Number(id));
      if (index < 0) return;
      const [row] = entries.splice(index, 1);
      if (editingEntryId === Number(id)) editingEntryId = null;
      pendingDelete = {row, index, remaining: 5, intervalId: null, timeoutId: null};
      pendingDelete.intervalId = setInterval(() => {
        if (!pendingDelete || pendingDelete.row.id !== row.id) return;
        pendingDelete.remaining -= 1;
        renderDeleteToast();
      }, 1000);
      pendingDelete.timeoutId = setTimeout(finalizePendingDelete, 5000);
      renderEntries();
      renderDeleteToast();
    }

    async function finalizePendingCategoryDelete() {
      if (!pendingCategoryDelete) return;
      const item = pendingCategoryDelete;
      pendingCategoryDelete = null;
      clearInterval(item.intervalId);
      clearTimeout(item.timeoutId);
      clearDeleteToast();
      try {
        await api('/api/category-delete', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({type: item.type, category: item.category})
        });
        await refresh();
      } catch (err) {
        renderCategoryRules();
        alert(err.message === 'not found' ? '后端接口未加载，请重启 Dashboard 后再删除。' : err.message);
      }
    }

    function undoPendingCategoryDelete() {
      if (!pendingCategoryDelete) return;
      const item = pendingCategoryDelete;
      pendingCategoryDelete = null;
      clearInterval(item.intervalId);
      clearTimeout(item.timeoutId);
      clearDeleteToast();
      renderCategoryRules();
    }

    function scheduleCategoryDelete(type, category) {
      if (pendingDelete) finalizePendingDelete();
      if (pendingDebtDelete) finalizePendingDebtDelete();
      if (pendingAccountDelete) finalizePendingAccountDelete();
      if (pendingCategoryDelete) finalizePendingCategoryDelete();
      if (editingCategoryKey === `${type}|||${category}`) editingCategoryKey = null;
      if (addingKeywordKey === `${type}|||${category}`) addingKeywordKey = null;
      pendingCategoryDelete = {kind: 'category', type, category, remaining: 5, intervalId: null, timeoutId: null};
      pendingCategoryDelete.intervalId = setInterval(() => {
        if (!pendingCategoryDelete || pendingCategoryDelete.type !== type || pendingCategoryDelete.category !== category) return;
        pendingCategoryDelete.remaining -= 1;
        renderDeleteToast();
      }, 1000);
      pendingCategoryDelete.timeoutId = setTimeout(finalizePendingCategoryDelete, 5000);
      renderCategoryRules();
      renderDeleteToast();
    }

    async function finalizePendingDebtDelete() {
      if (!pendingDebtDelete) return;
      const item = pendingDebtDelete;
      pendingDebtDelete = null;
      clearInterval(item.intervalId);
      clearTimeout(item.timeoutId);
      clearDeleteToast();
      try {
        await api('/api/account-debt-delete', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({id: item.row.id})
        });
        await refresh();
      } catch (err) {
        if (!debtBalances.some(row => Number(row.id) === Number(item.row.id))) {
          debtBalances.splice(Math.min(item.index, debtBalances.length), 0, item.row);
        }
        renderAssetManager();
        alert(err.message === 'not found' ? '后端接口未加载，请重启 Dashboard 后再删除。' : err.message);
      }
    }

    function undoPendingDebtDelete() {
      if (!pendingDebtDelete) return;
      const item = pendingDebtDelete;
      pendingDebtDelete = null;
      clearInterval(item.intervalId);
      clearTimeout(item.timeoutId);
      clearDeleteToast();
      if (!debtBalances.some(row => Number(row.id) === Number(item.row.id))) {
        debtBalances.splice(Math.min(item.index, debtBalances.length), 0, item.row);
      }
      renderAssetManager();
    }

    function scheduleDebtDelete(id) {
      if (pendingDelete) finalizePendingDelete();
      if (pendingCategoryDelete) finalizePendingCategoryDelete();
      if (pendingAccountDelete) finalizePendingAccountDelete();
      if (pendingDebtDelete) finalizePendingDebtDelete();
      const index = debtBalances.findIndex(row => Number(row.id) === Number(id));
      if (index < 0) return;
      const [row] = debtBalances.splice(index, 1);
      pendingDebtDelete = {kind: 'debt', row, index, remaining: 5, intervalId: null, timeoutId: null};
      pendingDebtDelete.intervalId = setInterval(() => {
        if (!pendingDebtDelete || pendingDebtDelete.row.id !== row.id) return;
        pendingDebtDelete.remaining -= 1;
        renderDeleteToast();
      }, 1000);
      pendingDebtDelete.timeoutId = setTimeout(finalizePendingDebtDelete, 5000);
      renderAssetManager();
      renderDeleteToast();
    }

    async function finalizePendingAccountDelete() {
      if (!pendingAccountDelete) return;
      const item = pendingAccountDelete;
      pendingAccountDelete = null;
      clearInterval(item.intervalId);
      clearTimeout(item.timeoutId);
      clearDeleteToast();
      try {
        await api('/api/account-delete', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({name: item.row.name})
        });
        await refresh();
      } catch (err) {
        if (!accountBalances.some(row => row.name === item.row.name)) {
          accountBalances.splice(Math.min(item.index, accountBalances.length), 0, item.row);
        }
        renderAssetManager();
        alert(err.message === 'cannot delete account with existing entries'
          ? '该账户已有账单或待确认记录，不能删除。'
          : err.message === 'not found'
            ? '后端接口未加载，请重启 Dashboard 后再删除。'
            : err.message);
      }
    }

    function undoPendingAccountDelete() {
      if (!pendingAccountDelete) return;
      const item = pendingAccountDelete;
      pendingAccountDelete = null;
      clearInterval(item.intervalId);
      clearTimeout(item.timeoutId);
      clearDeleteToast();
      if (!accountBalances.some(row => row.name === item.row.name)) {
        accountBalances.splice(Math.min(item.index, accountBalances.length), 0, item.row);
      }
      renderAssetManager();
    }

    function scheduleAccountDelete(name) {
      if (pendingDelete) finalizePendingDelete();
      if (pendingCategoryDelete) finalizePendingCategoryDelete();
      if (pendingDebtDelete) finalizePendingDebtDelete();
      if (pendingAccountDelete) finalizePendingAccountDelete();
      const index = accountBalances.findIndex(row => row.name === name);
      if (index < 0) return;
      const [row] = accountBalances.splice(index, 1);
      if (editingAccountName === name) editingAccountName = null;
      if (editingAccountBalanceName === name) editingAccountBalanceName = null;
      pendingAccountDelete = {kind: 'account', row, index, remaining: 5, intervalId: null, timeoutId: null};
      pendingAccountDelete.intervalId = setInterval(() => {
        if (!pendingAccountDelete || pendingAccountDelete.row.name !== row.name) return;
        pendingAccountDelete.remaining -= 1;
        renderDeleteToast();
      }, 1000);
      pendingAccountDelete.timeoutId = setTimeout(finalizePendingAccountDelete, 5000);
      renderAssetManager();
      renderDeleteToast();
    }

    function toneColor(tone) {
      const inverted = document.body.dataset.colorInverted === 'true';
      if (tone === 'income') return cssVar(inverted ? '--green' : '--red');
      if (tone === 'expense') return cssVar(inverted ? '--red' : '--green');
      return cssVar('--ink');
    }

    const pad2 = (n) => String(n).padStart(2, '0');
    const ymd = (date) => `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
    const slashDate = (value) => value ? value.replaceAll('-', '/') : '--/--/--';
    const entryDay = (entry) => String(entry.transaction_time || '').slice(0, 10);
    const parseDay = (value) => {
      const [year, month, day] = value.split('-').map(Number);
      return new Date(year, month - 1, day);
    };
    const addDays = (date, days) => {
      const next = new Date(date);
      next.setDate(next.getDate() + days);
      return next;
    };

    function table(rows, columns) {
      if (!rows.length) return '<div class="muted">暂无数据</div>';
      return '<table><thead><tr>' + columns.map(c => `<th>${c[1]}</th>`).join('') + '</tr></thead><tbody>' +
        rows.map(r => '<tr>' + columns.map(c => {
          const cls = c[3] ? ` class="${esc(c[3](r[c[0]], r))}"` : '';
          return `<td${cls}>${esc(c[2] ? c[2](r[c[0]], r) : r[c[0]])}</td>`;
        }).join('') + '</tr>').join('') +
        '</tbody></table>';
    }

    const isDebtKind = (row) => row.kind === 'liability' || row.kind === 'receivable';
    const debtBalanceCents = (row) => Number(row.computed_balance_cents ?? row.current_balance_cents ?? 0);

    // ---------- 资产管家（鲨鱼记账式分组 + 净资产卡） ----------
    // 三型派生（不加 debt_type 字段，纯前端呈现标签）：consume+locked→信用卡 / consume→借款 / repay→贷款
    function deriveDebtType(row) {
      const mode = String(row.expense_mode || '');
      const locked = Number(row.expense_mode_locked || 0) === 1;
      if (mode === 'repay') return '贷款';
      if (mode === 'consume') return locked ? '信用卡' : '借款';
      return '';
    }

    const assetTypeLabels = {cash: '现金', bank: '储蓄卡', virtual: '虚拟', custom: '自定义'};
    // 与后端 debt_accounts.CREDIT_PAYMENT_PATTERNS 保持一致（前端选「信用卡型」时校验名称）
    const CREDIT_NAME_PATTERNS = ['花呗', '抖音月付', '京东白条', '美团月付', '分付', '信用卡'];
    // 还款类分类（与后端 REPAY_TRANSFER_CATEGORIES 一致，平铺叶子名）
    const REPAY_CATEGORY_BY_NAME = [['房贷', '房贷'], ['车贷', '车贷']];
    const repayCategoryFor = (name) => {
      const text = String(name || '');
      const hit = REPAY_CATEGORY_BY_NAME.find(([key]) => text.includes(key));
      return hit ? hit[1] : '信用卡还款';
    };

    const ASSET_GROUP_DEFS = [
      {type: 'cash', icon: '💵', label: '现金'},
      {type: 'virtual', icon: '📱', label: '虚拟账户'},
      {type: 'bank', icon: '💳', label: '储蓄卡'},
      {type: 'credit', icon: '🏷', label: '信用卡'},
      {type: 'liability', icon: '🏦', label: '负债'},
      {type: 'receivable', icon: '🤝', label: '债权'},
      {type: 'custom', icon: '📦', label: '自定义资产'},
      {type: 'invest', icon: '📈', label: '投资账户'},
    ];
    const DEBT_GROUP_TYPES = new Set(['credit', 'liability', 'receivable']);
    const ASSET_GROUP_ORDER = {assetList: ['cash', 'virtual', 'bank'], debtList: ['credit', 'liability', 'receivable', 'custom', 'invest']};

    // 分组派生（零新字段）：归档账户不进主列表（净资产卡口径在后端，仍计入）
    function assetGroupRows() {
      const live = accountBalances.filter(row => Number(row.archived || 0) !== 1);
      const groups = {cash: [], virtual: [], bank: [], credit: [], liability: [], receivable: [], custom: [], invest: []};
      live.forEach(row => {
        if (row.kind === 'invest') { groups.invest.push(row); return; }
        if (row.kind === 'liability') {
          (deriveDebtType(row) === '信用卡' ? groups.credit : groups.liability).push(row);
          return;
        }
        if (row.kind === 'receivable') { groups.receivable.push(row); return; }
        const assetType = row.asset_type || 'custom';
        (groups[assetType] || groups.custom).push(row);
      });
      return groups;
    }

    // 净资产卡（三数字，口径在后端 net_worth：债权计资产侧、投资按市值、负债取绝对值）
    function netWorthCardHtml() {
      const nw = (summaryData && summaryData.net_worth) || {};
      const assetTotal = Number(nw.asset_total_cents || 0);
      const liabilityTotal = Number(nw.liability_total_cents || 0);
      const netWorth = Number(nw.net_worth_cents ?? (assetTotal - liabilityTotal));
      return `
        <div class="net-worth-card" id="netWorthCard" title="资产侧 = 现金/储蓄卡/虚拟/自定义资产 + 债权 + 投资市值（按市值）；负债侧 = 信用卡+负债欠款合计（取绝对值）">
          <div class="net-worth-main">
            <div class="net-worth-label">净资产</div>
            <div class="net-worth-value">${esc(money(netWorth))}</div>
          </div>
          <div class="net-worth-side">
            <span>资产 <strong>${esc(money(assetTotal))}</strong></span>
            <span>负债 <strong>${esc(money(liabilityTotal))}</strong></span>
          </div>
        </div>`;
    }

    function assetGroupSection(def, rows) {
      const isDebtGroup = DEBT_GROUP_TYPES.has(def.type);
      const subtotal = rows.reduce((s, r) => s + debtBalanceCents(r), 0);
      const subtitle = {credit: '（先消费后还款）', liability: '（我欠别人的）', receivable: '（别人欠我的）'}[def.type] || '';
      const subtotalNote = def.type === 'invest' ? '<span class="muted">（市值见理财页）</span>' : '';
      let html = `
        <div class="asset-group" id="asset-group-${def.type}">
        <div class="debt-group-head">
          <span class="debt-group-title">${def.icon} ${def.label}<span class="muted">${subtitle}</span></span>
          <span class="debt-group-subtotal">${esc(balanceMoney(subtotal))}${subtotalNote}</span>
        </div>`;
      if (rows.length) {
        html += `<table class="account-table">
          <thead><tr><th>账户</th><th>金额</th><th>币种</th><th></th></tr></thead>
          <tbody>${rows.map(row => isDebtGroup ? debtRowHtml(row) : assetRowHtml(row)).join('')}</tbody>
        </table>`;
      } else {
        html += '<div class="muted">暂无数据</div>';
      }
      return html + '</div>';
    }

    function assetRowHtml(row) {
      const value = row.computed_balance_cents ?? row.current_balance_cents;
      const isNameEditing = editingAccountName === row.name;
      const isBalanceEditing = editingAccountBalanceName === row.name;
      const isTypeEditing = editingAssetTypeName === row.name;
      const isInvest = row.kind === 'invest';
      const typeControl = isInvest
        ? '<span class="kind-badge asset-type" title="投资账户：市值与持仓在资产页投资组查看">📈 投资</span>'
        : (isTypeEditing
          ? `<select class="asset-type-select" data-asset-type-input="${esc(row.name)}" aria-label="账户类型">
              ${['cash', 'bank', 'virtual', 'custom'].map(t => `<option value="${t}" ${(row.asset_type || 'custom') === t ? 'selected' : ''}>${assetTypeLabels[t]}</option>`).join('')}
            </select>`
          : `<span class="kind-badge asset-type">${assetTypeLabels[row.asset_type] || assetTypeLabels.custom}</span>
             <button type="button" class="icon-button" title="纠正账户类型（仅影响分组归属）" aria-label="纠正账户类型" data-asset-type-edit="${esc(row.name)}">✎</button>`);
      return `
          <tr${isInvest ? ' class="invest-row"' : ''}>
            <td>
              <div class="account-name-cell">
                ${isNameEditing
                  ? `<input class="account-name-input" type="text" value="${esc(row.name)}" aria-label="账户名" data-account-name-input="${esc(row.name)}">`
                  : `<span>${esc(row.name)}</span>
                    ${typeControl}
                    ${isInvest ? '' : `<button type="button" class="icon-button" title="编辑账户名" aria-label="编辑账户名" data-account-name-edit="${esc(row.name)}">✎</button>`}`}
              </div>
            </td>
            <td class="money-cell debt-money">
              <div class="account-balance-cell">
                ${isBalanceEditing
                  ? `<input class="account-balance-input" type="text" inputmode="decimal" value="${esc(money(value))}" aria-label="编辑余额" data-account-balance-input="${esc(row.name)}">`
                  : `<span>${esc(balanceMoney(value))}</span>
                    ${isInvest ? '' : `<button type="button" class="icon-button" title="编辑余额" aria-label="编辑余额" data-account-balance-edit="${esc(row.name)}">✎</button>`}`}
              </div>
            </td>
            <td>${esc(row.currency)}</td>
            <td class="account-actions">
              <button type="button" class="text-delete" data-account-detail="${esc(row.name)}" title="跳转账单页查看该账户全部流水">明细</button>
              ${isInvest ? '' : `<button type="button" class="text-delete" data-account-delete="${esc(row.name)}">删除</button>`}
            </td>
          </tr>
        `;
    }

    // 三型徽章（仿鲨鱼记账）：信用卡 🔒 / 借款 / 贷款
    function debtTypeBadge(row) {
      const type = deriveDebtType(row);
      if (!type) return '';
      const titles = {
        '信用卡': '信用卡型：先消费后还款，还款走转账不计支出，无本金/利息概念',
        '借款': '借款型：消费口径，还款只走负债账户；本金+利息归档',
        '贷款': '贷款型：还款口径，月供转账+分类「房贷/车贷」计入支出',
      };
      return `<span class="kind-badge debttype" title="${esc(titles[type] || '')}">${type}${type === '信用卡' ? ' 🔒' : ''}</span>`;
    }

    // 借款型/贷款型利息预览副行（实时：Σ还款 − 本金）；本金未填引导补填；信用卡型无此副行
    function debtInterestLine(row) {
      const type = deriveDebtType(row);
      if (type !== '借款' && type !== '贷款') return '';
      if (editingPrincipalName === row.name) {
        return `
          <div class="debt-interest-line">
            <input class="account-balance-input" type="text" inputmode="decimal" min="0.01"
              value="${row.principal_cents != null ? esc(money(row.principal_cents)) : ''}"
              placeholder="本金(元，须为正数)" aria-label="编辑本金" data-principal-input="${esc(row.name)}">
          </div>`;
      }
      if (row.principal_cents == null) {
        return '<div class="debt-interest-line muted">未填本金，利息不可算（点「本金」补填）</div>';
      }
      const interest = row.interest_cents;
      return `<div class="debt-interest-line muted">本金 ${esc(money(row.principal_cents))} · 已还 ${esc(money(row.repaid_cents))} · 利息暂计 ${interest != null ? esc(money(interest)) : '—'}</div>`;
    }

    function debtRowHtml(row) {
      const value = row.computed_balance_cents ?? row.current_balance_cents;
      const isNameEditing = editingAccountName === row.name;
      const isBalanceEditing = editingAccountBalanceName === row.name;
      const paidOff = debtBalanceCents(row) === 0;
      const paidLabel = row.kind === 'liability' ? '已还清' : '已收齐';
      const badges =
        (row.kind === 'liability' ? debtTypeBadge(row) : '') +
        (row.kind === 'receivable' ? '<span class="kind-badge receivable" title="应收账户：余额为正，应收=正数">应收</span>' : '') +
        (paidOff ? `<span class="kind-badge paid" title="余额为 0，可以归档记入历史">✓${paidLabel}</span>` : '');
      const balanceEditTitle = row.kind === 'liability' ? '填入欠款数额（自动记为负余额，生成余额修正流水可追溯）' : '填入应收数额（自动记为正余额，生成余额修正流水可追溯）';
      const repayBtn = row.kind === 'liability'
        ? `<button type="button" class="text-delete" data-debt-repay="${esc(row.name)}" title="记一笔还款转账（从某个账户还到 ${esc(row.name)}），欠款自动减少">还款</button>`
        : `<button type="button" class="text-delete" data-debt-collect="${esc(row.name)}" title="记一笔收款转账（从 ${esc(row.name)} 收回到某个账户），应收自动减少">收款</button>`;
      const principalBtn = row.kind === 'liability' && ['借款', '贷款'].includes(deriveDebtType(row))
        ? `<button type="button" class="text-delete" data-principal-edit="${esc(row.name)}" title="编辑本金（利息 = Σ还款 − 本金）">本金</button>`
        : '';
      return `
          <tr>
            <td>
              <div class="account-name-cell">
                ${isNameEditing
                  ? `<input class="account-name-input" type="text" value="${esc(row.name)}" aria-label="账户名" data-account-name-input="${esc(row.name)}">`
                  : `<span>${esc(row.name)}</span>
                    ${badges}
                    <button type="button" class="icon-button" title="编辑账户名" aria-label="编辑账户名" data-account-name-edit="${esc(row.name)}">✎</button>`}
              </div>
              ${debtInterestLine(row)}
            </td>
            <td class="money-cell debt-money">
              <div class="account-balance-cell">
                ${isBalanceEditing
                  ? `<input class="account-balance-input" type="text" inputmode="decimal" value="${esc(money(value))}" aria-label="${balanceEditTitle}" data-account-balance-input="${esc(row.name)}">`
                  : `<span>${esc(balanceMoney(value))}</span>
                    <button type="button" class="icon-button" title="${balanceEditTitle}" aria-label="${balanceEditTitle}" data-account-balance-edit="${esc(row.name)}">✎</button>`}
              </div>
            </td>
            <td>${esc(row.currency)}</td>
            <td class="account-actions">
              ${repayBtn}
              ${principalBtn}
              <button type="button" class="text-delete" data-debt-detail="${esc(row.name)}" title="展开/收起该账户全部流水">${expandedDebtName === row.name ? '收起' : '明细'}</button>
              <button type="button" class="text-delete" data-account-archive="${esc(row.name)}" title="归档后不再出现在列表，相关流水保留，可随时在底部「已归档」里恢复">归档</button>
              <button type="button" class="text-delete" data-account-delete="${esc(row.name)}">删除</button>
            </td>
          </tr>
          ${expandedDebtName === row.name ? `<tr class="debt-detail-tr"><td colspan="4">${debtDetailHtml(row.name)}</td></tr>` : ''}
        `;
    }

    // 「+ 添加账户」：8 宫格类型选择 + 类型化表单（收敛原三处内联表单）
    const ADD_ACCOUNT_TYPES = [
      {type: 'cash', icon: '💵', label: '现金'},
      {type: 'bank', icon: '💳', label: '储蓄卡'},
      {type: 'credit', icon: '🏷', label: '信用卡'},
      {type: 'virtual', icon: '📱', label: '虚拟账户'},
      {type: 'invest', icon: '📈', label: '投资账户'},
      {type: 'debt', icon: '🏦', label: '负债'},
      {type: 'receivable', icon: '🤝', label: '债权'},
      {type: 'custom', icon: '📦', label: '自定义资产'},
    ];

    function addAccountPanelHtml() {
      const grid = ADD_ACCOUNT_TYPES.map(t =>
        `<button type="button" class="add-type-cell" data-add-type="${t.type}"><span class="add-type-icon">${t.icon}</span><span>${t.label}</span></button>`
      ).join('');
      let form = '';
      if (addingAccountType === 'credit') {
        form = `
          <div class="add-account-form">
            <input id="newAccName" type="text" placeholder="名称（需含 花呗/白条/月付/分付/信用卡）">
            <input id="newAccAmount" type="text" inputmode="decimal" placeholder="欠款金额">
            <button type="button" class="primary" id="submitNewAccount">添加</button>
            <div class="muted add-account-hint">信用卡型：固定消费口径并锁定，无本金/利息概念</div>
          </div>`;
      } else if (addingAccountType === 'debt') {
        form = `
          <div class="add-account-form">
            <input id="newAccName" type="text" placeholder="名称（如 房贷/车贷/借款-XX）">
            <input id="newAccAmount" type="text" inputmode="decimal" placeholder="欠款金额">
            <label class="add-type-radio"><input type="radio" name="newDebtMode" value="consume" checked> 借款型</label>
            <label class="add-type-radio"><input type="radio" name="newDebtMode" value="repay"> 贷款型</label>
            <input id="newAccPrincipal" type="text" inputmode="decimal" min="0.01" placeholder="本金（选填，元，须为正数）">
            <button type="button" class="primary" id="submitNewAccount">添加</button>
            <div class="muted add-account-hint">贷款型：月供转账+分类「房贷/车贷」计入支出；名称含 房贷/车贷 自动选贷款型</div>
          </div>`;
      } else if (addingAccountType === 'receivable') {
        form = `
          <div class="add-account-form">
            <input id="newAccName" type="text" placeholder="名称（如 爸爸借）">
            <input id="newAccAmount" type="text" inputmode="decimal" placeholder="应收金额">
            <button type="button" class="primary" id="submitNewAccount">添加</button>
          </div>`;
      } else if (addingAccountType) {
        form = `
          <div class="add-account-form">
            <input id="newAccName" type="text" placeholder="名称">
            <input id="newAccAmount" type="text" inputmode="decimal" placeholder="初始余额">
            <button type="button" class="primary" id="submitNewAccount">添加</button>
          </div>`;
      }
      return `
        <div class="add-account-panel">
          <button type="button" class="secondary" id="toggleAddAccount">+ 添加账户</button>
          <div class="add-type-grid${addingAccountType ? '' : ' hidden'}" id="addTypeGrid">${grid}</div>
          ${form}
        </div>`;
    }

    // 债权负债区（鲨鱼记账式）：归档的收进底部折叠区（参考活动归档）
    let showArchivedDebts = false;
    // 债权/负债明细展开态（与活动明细同款交互，同一时间只展开一个）
    let expandedDebtName = null;

    function debtArchivedCardHtml(row) {
      const type = deriveDebtType(row);
      // 借款型/贷款型：归档快照三数字（本金/总还款/总利息）；信用卡型维持现状样式
      const snapshotLine = (type === '借款' || type === '贷款')
        ? `<div class="debt-snapshot muted">本金 ${row.archived_principal_cents != null ? esc(money(row.archived_principal_cents)) : '未填'}
            · 已还 ${esc(money(row.archived_repay_cents || 0))}
            · 总利息 ${row.archived_interest_cents != null ? esc(money(row.archived_interest_cents)) : '—'}</div>`
        : '';
      return `
        <div class="activity-card is-archived">
          <div class="activity-head">
            <div class="activity-title">
              <strong>${esc(row.name)}</strong>
              <span class="muted">${row.kind === 'liability' ? '负债' : '债权'} · 归档时余额以编辑为准</span>
            </div>
            <div class="activity-money">${esc(balanceMoney(debtBalanceCents(row)))}</div>
            <div class="activity-actions">
              <button class="secondary" data-debt-detail="${esc(row.name)}" title="展开/收起该账户全部流水">${expandedDebtName === row.name ? '收起' : '明细'}</button>
              <button class="secondary" data-debt-restore="${esc(row.name)}" title="恢复到列表">恢复</button>
              <button class="secondary" data-account-delete="${esc(row.name)}">删除</button>
            </div>
          </div>
          ${snapshotLine}
          ${expandedDebtName === row.name ? debtDetailHtml(row.name) : ''}
        </div>`;
    }

    // 债权/负债明细（与活动明细同款交互）：数据都在内存（entries 全量），直接同步渲染，无需请求
    function debtDetailHtml(name) {
      const account = accountBalances.find(r => r.name === name);
      const rows = entries
        .filter(e => e.account === name || e.target_account === name)
        .sort((a, b) => String(b.transaction_time || '').localeCompare(String(a.transaction_time || '')));
      // 金额按"该账户余额变动方向"显示：转入(+)/转出(-)，与金额列同一符号口径
      const deltaFor = e => {
        if (e.type === '转账') {
          if (e.target_account === name) return e.account === name ? -Number(e.amount_cents) : Number(e.amount_cents);
          return e.account === name ? -Number(e.amount_cents) : 0;
        }
        if (e.type === '余额修正') return Number(e.balance_delta_cents || 0);
        if (e.account !== name) return 0;
        return toneIncreaseTypes.has(e.type) ? Number(e.amount_cents) : toneDecreaseTypes.has(e.type) ? -Number(e.amount_cents) : 0;
      };
      const rowsHtml = rows.map(e => {
        const delta = deltaFor(e);
        const isTransfer = e.type === '转账';
        const cat = isTransfer
          ? (e.category && e.category !== '转账' ? e.category : '转账')
          : (e.type === '余额修正' ? '余额修正' : (categoryLabel(e.category) || e.type));
        const obj = isTransfer
          ? `${e.account} → ${e.target_account}`
          : (e.note || e.transaction_object || '');
        const tone = delta > 0 ? 'income' : delta < 0 ? 'expense' : 'neutral-money';
        return `
          <div class="activity-row">
            <span class="activity-time">${esc(String(e.transaction_time || '').slice(0, 16))}</span>
            <span class="activity-cat">${esc(cat)}</span>
            <span class="activity-obj" title="${esc(obj)}">${esc(obj)}</span>
            <span class="activity-amt ${tone}">${delta === 0 ? '0.00' : signedMoney(delta, delta > 0 ? 'income' : 'expense')}</span>
          </div>`;
      }).join('');
      const balance = account ? balanceMoney(account.computed_balance_cents ?? account.current_balance_cents) : '—';
      return `
        <div class="activity-detail">
          ${rowsHtml || '<div class="muted">该账户还没有流水</div>'}
          <div class="activity-sum muted">共 ${rows.length} 笔 · 当前余额 ${esc(balance)}</div>
        </div>`;
    }

    function renderDebtSection() {
      const groups = assetGroupRows();
      let html = ASSET_GROUP_ORDER.debtList
        .map(type => assetGroupSection(ASSET_GROUP_DEFS.find(def => def.type === type), groups[type]))
        .join('');
      if (debtBalances.length) html += renderDebtBalances(debtBalances);
      const archivedRows = accountBalances.filter(row => isDebtKind(row) && Number(row.archived || 0) === 1);
      if (archivedRows.length) {
        html += `
          <button class="activity-archived-toggle" id="debtArchivedToggle">
            已归档债权/负债（${archivedRows.length}）<span class="archived-arrow">${showArchivedDebts ? '▴' : '▾'}</span>
          </button>
          <div class="activity-archived-area ${showArchivedDebts ? '' : 'hidden'}" id="debtArchivedArea">
            ${showArchivedDebts ? archivedRows.map(debtArchivedCardHtml).join('') : ''}
          </div>`;
      }
      return html;
    }

    function renderAssetGroups() {
      const groups = assetGroupRows();
      return ASSET_GROUP_ORDER.assetList
        .map(type => assetGroupSection(ASSET_GROUP_DEFS.find(def => def.type === type), groups[type]))
        .join('');
    }

    // 资产管家总入口：净资产卡 + 添加入口 + 8 组渲染（assetList=现金/虚拟/储蓄卡，debtList=其余组+归档折叠区）
    function renderAssetManager() {
      if (!$('assetList')) return;
      if ($('assetManager')) $('assetManager').innerHTML = netWorthCardHtml() + addAccountPanelHtml();
      $('assetList').innerHTML = renderAssetGroups();
      if ($('debtList')) $('debtList').innerHTML = renderDebtSection();
      bindAccountActions();
      bindDebtActions();
      bindAddAccountPanel();
    }

    async function saveAccountName(input, oldName) {
      if (input.dataset.saving === 'true') return;
      const newName = input.value.trim();
      editingAccountName = null;
      if (!newName || newName === oldName) {
        renderAssetManager();
        return;
      }
      input.dataset.saving = 'true';
      await api('/api/account-rename', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({old_name: oldName, new_name: newName})
      });
      await refresh();
    }

    async function saveAccountBalance(input, name) {
      if (input.dataset.saving === 'true') return;
      const actual = input.value.trim();
      editingAccountBalanceName = null;
      if (!actual) {
        renderAssetManager();
        return;
      }
      input.dataset.saving = 'true';
      // 债权/负债行：✎ 填欠款/应收数额 → 转换符号走余额修正（生成可见流水，与资产口径一致）
      const account = accountBalances.find(row => row.name === name);
      if (account && isDebtKind(account)) {
        const x = Math.abs(parseFloat(actual.replace(/[,-]/g, '')) || 0);
        const signed = account.kind === 'liability' ? -x : x;
        await api('/api/correct-balance', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({account: name, actual: signed.toFixed(2), note: `填入${account.kind === 'liability' ? '欠款' : '应收'}数额`})
        });
      } else {
        await api('/api/correct-balance', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({account: name, actual})
        });
      }
      await refresh();
    }

    function bindAccountActions() {
      document.querySelectorAll('[data-account-name-edit]').forEach(btn => {
        btn.onclick = () => {
          editingAccountName = btn.dataset.accountNameEdit;
          editingAccountBalanceName = null;
          renderAssetManager();
          document.querySelector(`[data-account-name-input="${CSS.escape(editingAccountName)}"]`)?.focus();
        };
      });
      document.querySelectorAll('[data-account-name-input]').forEach(input => {
        input.onkeydown = event => {
          if (event.key === 'Enter') saveAccountName(input, input.dataset.accountNameInput).catch(err => alert(err.message));
          if (event.key === 'Escape') {
            editingAccountName = null;
            renderAssetManager();
          }
        };
        input.onblur = () => saveAccountName(input, input.dataset.accountNameInput).catch(err => {
          alert(err.message === 'account already exists' ? '账户名已存在。' : err.message);
          renderAssetManager();
        });
      });
      document.querySelectorAll('[data-account-balance-edit]').forEach(btn => {
        btn.onclick = () => {
          editingAccountBalanceName = btn.dataset.accountBalanceEdit;
          editingAccountName = null;
          renderAssetManager();
          document.querySelector(`[data-account-balance-input="${CSS.escape(editingAccountBalanceName)}"]`)?.focus();
        };
      });
      document.querySelectorAll('[data-account-balance-input]').forEach(input => {
        input.onkeydown = event => {
          if (event.key === 'Enter') saveAccountBalance(input, input.dataset.accountBalanceInput).catch(err => alert(err.message));
          if (event.key === 'Escape') {
            editingAccountBalanceName = null;
            renderAssetManager();
          }
        };
        input.onblur = () => saveAccountBalance(input, input.dataset.accountBalanceInput).catch(err => {
          alert(err.message);
          renderAssetManager();
        });
      });
      document.querySelectorAll('[data-account-delete]').forEach(btn => {
        btn.onclick = () => scheduleAccountDelete(btn.dataset.accountDelete);
      });
      // 明细推广到资产行：跳转账单页按该账户筛选（与负债行一致）
      document.querySelectorAll('[data-account-detail]').forEach(btn => {
        btn.onclick = () => jumpToEntriesByAccount(btn.dataset.accountDetail);
      });
      // asset_type 纠正入口（仅 asset kind；猜错兜底：存量为 custom 不影响统计只影响分组）
      document.querySelectorAll('[data-asset-type-edit]').forEach(btn => {
        btn.onclick = () => {
          editingAssetTypeName = btn.dataset.assetTypeEdit;
          renderAssetManager();
        };
      });
      document.querySelectorAll('[data-asset-type-input]').forEach(select => {
        select.onchange = async () => {
          const name = select.dataset.assetTypeInput;
          editingAssetTypeName = null;
          try {
            await api('/api/account-set', {
              method: 'POST', headers: {'content-type': 'application/json'},
              body: JSON.stringify({name, asset_type: select.value}),
            });
            await refresh();
          } catch (err) {
            alert(err.message);
            renderAssetManager();
          }
        };
      });
      // 投资账户行已在资产页投资组内展示；V1 关闭理财 Web 页，不再跳转
    }

    // 「+ 添加账户」8 宫格 + 类型化表单（收敛原三处内联添加表单）
    function bindAddAccountPanel() {
      const toggle = $('toggleAddAccount');
      if (toggle) toggle.onclick = () => {
        const grid = $('addTypeGrid');
        if (grid) grid.classList.toggle('hidden');
      };
      document.querySelectorAll('[data-add-type]').forEach(cell => {
        cell.onclick = () => {
          const type = cell.dataset.addType;
          if (type === 'invest') {
            // V1 关闭理财 Web 页：只提示、不跳转
            $('status').textContent = '投资账户请在理财页添加';
            return;
          }
          addingAccountType = type;
          renderAssetManager();
          $('newAccName')?.focus();
        };
      });
      const submit = $('submitNewAccount');
      if (submit) submit.onclick = () => submitNewAccount().catch(err => alert(err.message));
      ['newAccName', 'newAccAmount', 'newAccPrincipal'].forEach(id => {
        if ($(id)) $(id).onkeydown = event => {
          if (event.key === 'Enter') submitNewAccount().catch(err => alert(err.message));
        };
      });
      // 负债类型默认：名称含 房贷/车贷 → 自动选贷款型
      const nameInput = $('newAccName');
      if (nameInput && addingAccountType === 'debt') {
        nameInput.oninput = () => {
          const repayRadio = document.querySelector('input[name="newDebtMode"][value="repay"]');
          if (repayRadio) repayRadio.checked = /房贷|车贷/.test(nameInput.value);
        };
      }
    }

    async function submitNewAccount() {
      const type = addingAccountType;
      if (!type || type === 'invest') return;
      const name = $('newAccName')?.value.trim() || '';
      const amount = Math.abs(parseFloat(($('newAccAmount')?.value || '').replace(/[,-]/g, '')) || 0);
      if (!name) { $('status').textContent = '请输入名称'; return; }
      if (!amount) { $('status').textContent = '请输入金额'; return; }
      let payload;
      if (type === 'credit') {
        // 信用卡型：名称必须命中信用支付方式（后端会强制 consume+锁定）
        if (!CREDIT_NAME_PATTERNS.some(pattern => name.includes(pattern))) {
          $('status').textContent = '名称需含 花呗/白条/月付/分付/信用卡，才能创建信用卡型账户';
          return;
        }
        payload = {name, balance: -amount, kind: 'liability', expense_mode: 'consume'};
      } else if (type === 'debt') {
        const mode = document.querySelector('input[name="newDebtMode"]:checked')?.value === 'repay' ? 'repay' : 'consume';
        payload = {name, balance: -amount, kind: 'liability', expense_mode: mode};
        const principal = $('newAccPrincipal')?.value.trim();
        if (principal) {
          // 本金必须为正数（与后端校验一致；填 0 多半是误操作，直接拒绝）
          const principalValue = parseFloat(principal.replace(/[,-]/g, ''));
          if (!principalValue || principalValue <= 0) {
            $('status').textContent = '本金必须为正数';
            return;
          }
          payload.principal = principal;
        }
      } else if (type === 'receivable') {
        payload = {name, balance: amount, kind: 'receivable'};
      } else {
        payload = {name, balance: amount, kind: 'asset', asset_type: type};
      }
      await api('/api/account-set', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify(payload),
      });
      addingAccountType = null;
      await refresh();
    }

    function renderDebtBalances(rows) {
      if (!rows.length) return '<div class="muted">暂无数据</div>';
      return `<table class="debt-table">
        <thead><tr><th>交易对象</th><th>应收</th><th>应付</th><th></th></tr></thead>
        <tbody>${rows.map(row => `
          <tr>
            <td>${esc(row.name)}</td>
            <td class="money-cell debt-money">${esc(money(row.receivable_cents))}</td>
            <td class="money-cell debt-money">${esc(money(row.payable_cents))}</td>
            <td class="debt-actions"><button class="text-delete" data-debt-delete="${esc(row.id)}">删除</button></td>
          </tr>
        `).join('')}</tbody>
      </table>`;
    }

    function bindDebtActions() {
      document.querySelectorAll('[data-debt-delete]').forEach(btn => {
        btn.onclick = () => scheduleDebtDelete(Number(btn.dataset.debtDelete));
      });
      // 本金编辑（借款型/贷款型）：行内小输入框，走 /api/account-set 只传 principal
      document.querySelectorAll('[data-principal-edit]').forEach(btn => {
        btn.onclick = () => {
          editingPrincipalName = btn.dataset.principalEdit;
          editingAccountName = null;
          editingAccountBalanceName = null;
          renderAssetManager();
          document.querySelector(`[data-principal-input="${CSS.escape(editingPrincipalName)}"]`)?.focus();
        };
      });
      document.querySelectorAll('[data-principal-input]').forEach(input => {
        const savePrincipal = async () => {
          const name = input.dataset.principalInput;
          const value = input.value.trim();
          editingPrincipalName = null;
          if (!value) { renderAssetManager(); return; }
          // 本金必须为正数（与后端校验一致；填 0 多半是误操作，直接拒绝）
          const principalValue = parseFloat(value.replace(/[,-]/g, ''));
          if (!principalValue || principalValue <= 0) {
            $('status').textContent = '本金必须为正数';
            renderAssetManager();
            return;
          }
          try {
            await api('/api/account-set', {
              method: 'POST', headers: {'content-type': 'application/json'},
              body: JSON.stringify({name, principal: value}),
            });
            await refresh();
          } catch (err) {
            alert(err.message);
            renderAssetManager();
          }
        };
        input.onkeydown = event => {
          if (event.key === 'Enter') savePrincipal().catch(err => alert(err.message));
          if (event.key === 'Escape') { editingPrincipalName = null; renderAssetManager(); }
        };
        input.onblur = () => savePrincipal().catch(err => { alert(err.message); renderAssetManager(); });
      });
      // 归档：借款型/贷款型先弹「本金/已还/利息」三项核对（软兜底：余额修正抹平的欠款不计入已还）
      document.querySelectorAll('[data-account-archive]').forEach(btn => {
        btn.onclick = async () => {
          const name = btn.dataset.accountArchive;
          const row = accountBalances.find(r => r.name === name);
          const dtype = row ? deriveDebtType(row) : '';
          let message = `归档「${name}」？\n归档后不再显示在列表，相关流水保留，可随时在下方「已归档」里恢复。`;
          if (dtype === '借款' || dtype === '贷款') {
            const principalText = row.principal_cents != null ? money(row.principal_cents) : '未填';
            const interestText = row.interest_cents != null ? money(row.interest_cents) : '不可算';
            message = `归档「${name}」？将写入快照：\n本金 ${principalText} · 已还 ${money(row.repaid_cents || 0)} · 总利息 ${interestText}\n（余额修正抹平的欠款不计入已还，请核对）\n归档后不再显示在列表，可随时恢复。`;
          }
          if (!confirm(message)) return;
          try {
            await api('/api/account-archive', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({name, archived: true})});
            await refresh();
          } catch (err) { $('status').textContent = err.message; }
        };
      });
      document.querySelectorAll('[data-debt-restore]').forEach(btn => {
        btn.onclick = async () => {
          try {
            await api('/api/account-archive', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({name: btn.dataset.debtRestore, archived: false})});
            await refresh();
          } catch (err) { $('status').textContent = err.message; }
        };
      });
      // 明细：与活动明细一致，直接在行/卡片下方展开该账户全部流水（不再跳转账单页）
      document.querySelectorAll('[data-debt-detail]').forEach(btn => {
        btn.onclick = () => {
          const name = btn.dataset.debtDetail;
          expandedDebtName = expandedDebtName === name ? null : name;
          renderAssetManager();
        };
      });
      // 还款/收款：预填快速记账转账（手动输入金额），保存后欠款/应收自动减少；
      // 关联已有还款流水则直接在「明细」里找到该笔，编辑转入/转出账户到本账户即可
      const defaultTransferCounterparty = () => {
        const assets = accountBalances.filter(r => !isDebtKind(r) && r.kind !== 'invest').map(r => r.name);
        return assets.includes('余额') ? '余额' : (assets[0] || '余额');
      };
      const prefillDebtTransfer = (from, to, hint) => {
        $('quickForm').classList.remove('hidden');
        $('fType').value = '转账';
        $('fType').dispatchEvent(new Event('change'));
        if ([...$('fAccount').options].some(o => o.value === from)) $('fAccount').value = from;
        if ([...$('fTargetAccount').options].some(o => o.value === to)) $('fTargetAccount').value = to;
        // 贷款型月供：预填还款类分类（房贷/车贷/信用卡还款），月供计入当月支出
        const target = accountBalances.find(r => r.name === to);
        if (target && target.kind === 'liability' && deriveDebtType(target) === '贷款') {
          $('fCategory').value = repayCategoryFor(to);
        }
        $('quickForm').scrollIntoView({behavior: 'smooth', block: 'start'});
        $('fAmount').focus();
        $('status').textContent = hint;
      };
      document.querySelectorAll('[data-debt-repay]').forEach(btn => {
        btn.onclick = () => prefillDebtTransfer(defaultTransferCounterparty(), btn.dataset.debtRepay,
          `记还款：输入金额后点「保存」（转账 → ${btn.dataset.debtRepay}，欠款自动减少）`);
      });
      document.querySelectorAll('[data-debt-collect]').forEach(btn => {
        btn.onclick = () => prefillDebtTransfer(btn.dataset.debtCollect, defaultTransferCounterparty(),
          `记收款：输入金额后点「保存」（转账 ${btn.dataset.debtCollect} → 收回账户，应收自动减少）`);
      });
      const toggle = $('debtArchivedToggle');
      if (toggle) toggle.onclick = () => { showArchivedDebts = !showArchivedDebts; renderAssetManager(); };
    }

    const trendHoverPoints = {};
    const pieHoverRegions = {};
    let chartAnimationToken = 0;
    let chartResizeTimer = null;

    function cssVar(name) {
      return getComputedStyle(document.body).getPropertyValue(name).trim();
    }

    function chartFont(size, weight = 500) {
      return `${weight} ${size}px ui-monospace, SFMono-Regular, Menlo, Consolas, "PingFang SC", "Microsoft YaHei", monospace`;
    }

    function prepareCanvas(canvas, cssWidth, cssHeight, displayWidth = cssWidth, displayHeight = cssHeight) {
      const ratio = window.devicePixelRatio || 1;
      canvas.style.width = `${displayWidth}px`;
      canvas.style.height = `${displayHeight}px`;
      canvas.width = Math.round(cssWidth * ratio);
      canvas.height = Math.round(cssHeight * ratio);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      return {ctx, width: cssWidth, height: cssHeight};
    }

    function chartDisplaySize(canvas, square = false) {
      const wrap = canvas.parentElement;
      const width = Math.max(1, wrap.clientWidth);
      const height = Math.max(1, wrap.clientHeight);
      if (!square) return {width, height};
      const size = Math.min(width, height);
      return {width: size, height: size};
    }

    function textPill(ctx, text, x, y, align, color, size = 11, weight = 500) {
      ctx.font = chartFont(size, weight);
      const metrics = ctx.measureText(text);
      const width = metrics.width + 12;
      const height = size + 7;
      const left = align === 'right' ? x - width : align === 'center' ? x - width / 2 : x;
      roundedRect(ctx, left, y - height + 4, width, height, 5);
      ctx.fillStyle = cssVar('--panel');
      ctx.globalAlpha = .94;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.fillStyle = color;
      ctx.textAlign = align;
      ctx.fillText(text, x + (align === 'right' ? -6 : align === 'center' ? 0 : 6), y);
    }

    function roundedRect(ctx, x, y, width, height, radius) {
      ctx.beginPath();
      ctx.moveTo(x + radius, y);
      ctx.arcTo(x + width, y, x + width, y + height, radius);
      ctx.arcTo(x + width, y + height, x, y + height, radius);
      ctx.arcTo(x, y + height, x, y, radius);
      ctx.arcTo(x, y, x + width, y, radius);
      ctx.closePath();
    }

    function drawSmoothLine(ctx, points) {
      ctx.beginPath();
      points.forEach((point, index) => {
        if (index === 0) {
          ctx.moveTo(point.x, point.y);
          return;
        }
        const prev = points[index - 1];
        const midX = (prev.x + point.x) / 2;
        ctx.bezierCurveTo(midX, prev.y, midX, point.y, point.x, point.y);
      });
    }

    function drawTrend(canvasId, points, progress = 1) {
      const canvas = $(canvasId);
      const display = chartDisplaySize(canvas);
      const {ctx, width, height} = prepareCanvas(canvas, display.width, display.height);
      ctx.clearRect(0, 0, width, height);
      const pad = {left: 58, right: 26, top: 18, bottom: 34};
      const chartW = width - pad.left - pad.right;
      const chartH = height - pad.top - pad.bottom;
      roundedRect(ctx, 8, 8, width - 16, height - 16, 8);
      ctx.fillStyle = colorMixPanel();
      ctx.fill();
      ctx.strokeStyle = cssVar('--line');
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i += 1) {
        const y = pad.top + chartH * i / 4;
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
      }
      if (!points.length) {
        ctx.fillStyle = cssVar('--muted');
        ctx.font = chartFont(14);
        ctx.textAlign = 'center';
        ctx.fillText('暂无数据', width / 2, height / 2);
        return;
      }
      const values = points.flatMap(p => [Number(p.income_cents || 0), Number(p.expense_cents || 0)]);
      const max = Math.max(...values, 100);
      const step = chartW / Math.max(points.length - 1, 1);
      trendHoverPoints[canvasId] = [];
      const zeroY = pad.top + chartH / 2;
      ctx.strokeStyle = cssVar('--ink');
      ctx.globalAlpha = .7;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.left, zeroY);
      ctx.lineTo(width - pad.right, zeroY);
      ctx.stroke();
      ctx.globalAlpha = 1;

      [
        {key: 'income_cents', label: '收入', tone: 'income', direction: -1},
        {key: 'expense_cents', label: '支出', tone: 'expense', direction: 1},
      ].forEach(seriesConfig => {
        const color = toneColor(seriesConfig.tone);
        const series = points.map((p, i) => ({
          x: pad.left + i * step,
          y: zeroY + seriesConfig.direction * (Number(p[seriesConfig.key] || 0) / max) * (chartH / 2 - 12) * progress,
          value: Number(p[seriesConfig.key] || 0),
          day: p.day,
          label: seriesConfig.label,
          tone: seriesConfig.tone,
        }));
        drawSmoothLine(ctx, series);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.4;
        ctx.stroke();
        const avg = series.reduce((sum, point) => sum + point.value, 0) / Math.max(series.length, 1);
        const avgY = zeroY + seriesConfig.direction * (avg / max) * (chartH / 2 - 12) * progress;
        ctx.save();
        ctx.setLineDash([6, 6]);
        ctx.strokeStyle = color;
        ctx.globalAlpha = .58;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pad.left, avgY);
        ctx.lineTo(width - pad.right, avgY);
        ctx.stroke();
        ctx.restore();
        const labelY = avgY + (seriesConfig.direction < 0 ? -10 : 22);
        textPill(ctx, `${seriesConfig.label}日均 ${money(avg)}`, width - pad.right - 4, labelY, 'right', color);
        ctx.globalAlpha = 1;
        series.forEach(point => {
          trendHoverPoints[canvasId].push(point);
        });
      });
      ctx.font = chartFont(12);
      ctx.fillStyle = cssVar('--muted');
      ctx.textAlign = 'center';
      points.filter((_, i) => i === 0 || i === points.length - 1 || i % Math.ceil(points.length / 5) === 0).forEach(p => {
        const index = points.indexOf(p);
        ctx.fillText(String(p.day || '').slice(5).replace('-', '/'), pad.left + index * step, height - 16);
      });
    }

    function colorMixPanel() {
      return document.body.dataset.theme === 'dark' ? 'rgba(255,255,255,.025)' : 'rgba(255,255,255,.42)';
    }

    const pieColors = ['#2dd4bf', '#5b8def', '#f5b84d', '#a78bfa', '#4ade80', '#f472b6', '#38bdf8', '#fb923c', '#94a3b8', '#818cf8'];

    function drawPie(canvasId, rows, tone, progress = 1) {
      const canvas = $(canvasId);
      const display = chartDisplaySize(canvas);
      const {ctx, width, height} = prepareCanvas(canvas, display.width, display.height);
      ctx.clearRect(0, 0, width, height);
      pieHoverRegions[canvasId] = [];
      const total = rows.reduce((sum, row) => sum + Number(row.amount_cents || 0), 0);
      if (!total) {
        ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--muted').trim();
        ctx.font = chartFont(14);
        ctx.textAlign = 'center';
        ctx.fillText('暂无数据', width / 2, height / 2);
        return;
      }
      const cx = width / 2;
      const cy = height / 2;
      const radius = Math.max(44, Math.min(height * .31, width * .18));
      const innerRadius = radius * .6;
      let start = -Math.PI / 2;
      const labels = [];
      rows.forEach((row, index) => {
        const value = Number(row.amount_cents || 0);
        const percent = value / total;
        const angle = percent * Math.PI * 2 * progress;
        const end = start + angle;
        if (progress > .98) {
          pieHoverRegions[canvasId].push({start, end, row, percent, tone, cx, cy, innerRadius, radius});
        }
        ctx.beginPath();
        ctx.arc(cx, cy, radius, start, end);
        ctx.arc(cx, cy, innerRadius, end, start, true);
        ctx.closePath();
        ctx.fillStyle = pieColors[index % pieColors.length];
        ctx.fill();
        const fullMid = start + (percent * Math.PI * 2) / 2;
        labels.push({
          row,
          percent,
          color: pieColors[index % pieColors.length],
          mid: fullMid,
          side: Math.cos(fullMid) >= 0 ? 'right' : 'left',
          sourceX: cx + Math.cos(fullMid) * (radius + 4),
          sourceY: cy + Math.sin(fullMid) * (radius + 4),
          elbowX: cx + Math.cos(fullMid) * (radius + 30),
          elbowY: cy + Math.sin(fullMid) * (radius + 30),
          labelY: cy + Math.sin(fullMid) * (radius + 42),
        });
        start += percent * Math.PI * 2 * progress;
      });
      // 中心合计：垫一块面板色底板，避免红绿字压在环上糊掉
      textPill(ctx, money(total * progress), cx, cy + 8, 'center', toneColor(tone), 22, 800);
      if (progress > .98) drawPieLabels(ctx, {width, height}, labels.filter(label => label.percent >= .035));
    }

    function distributeLabels(labels, canvasHeight) {
      const sorted = labels.slice().sort((a, b) => a.labelY - b.labelY);
      const gap = 26;
      const minY = 26;
      const maxY = canvasHeight - 26;
      sorted.forEach(label => {
        label.labelY = Math.max(minY, Math.min(maxY, label.labelY));
      });
      for (let i = 1; i < sorted.length; i += 1) {
        if (sorted[i].labelY - sorted[i - 1].labelY < gap) {
          sorted[i].labelY = sorted[i - 1].labelY + gap;
        }
      }
      const overflow = sorted.length ? sorted.at(-1).labelY - maxY : 0;
      if (overflow > 0) {
        sorted.forEach(label => {
          label.labelY -= overflow;
        });
      }
      for (let i = sorted.length - 2; i >= 0; i -= 1) {
        if (sorted[i + 1].labelY - sorted[i].labelY < gap) {
          sorted[i].labelY = sorted[i + 1].labelY - gap;
        }
      }
      sorted.forEach(label => {
        label.labelY = Math.max(minY, Math.min(maxY, label.labelY));
      });
      return sorted;
    }

    function drawPieLabels(ctx, canvas, labels) {
      const sides = {
        left: distributeLabels(labels.filter(label => label.side === 'left'), canvas.height),
        right: distributeLabels(labels.filter(label => label.side === 'right'), canvas.height),
      };
      ctx.font = chartFont(12, 650);
      Object.entries(sides).forEach(([side, sideLabels]) => {
        const right = side === 'right';
        const labelInset = Math.min(190, Math.max(110, canvas.width * .28));
        const lineX = right ? canvas.width - labelInset : labelInset;
        const textX = right ? lineX + 8 : lineX - 8;
        sideLabels.forEach(label => {
          const name = categoryLabel(label.row.category || '未分类');
          const percent = `${(label.percent * 100).toFixed(1)}%`;
          ctx.strokeStyle = label.color;
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          ctx.moveTo(label.sourceX, label.sourceY);
          ctx.lineTo(label.elbowX, label.elbowY);
          ctx.lineTo(lineX, label.labelY);
          ctx.stroke();
          ctx.fillStyle = cssVar('--ink-soft');
          ctx.textAlign = right ? 'left' : 'right';
          ctx.fillText(`${name} ${percent}`, textX, label.labelY + 4);
        });
      });
    }

    function animateOverviewCharts(analysis) {
      const token = ++chartAnimationToken;
      const start = performance.now();
      const duration = 620;
      const easeOut = (t) => 1 - Math.pow(1 - t, 3);
      function frame(now) {
        if (token !== chartAnimationToken) return;
        const progress = easeOut(Math.min((now - start) / duration, 1));
        drawTrend('cashflowTrend', analysis.trend, progress);
        drawPie('expensePie', analysis.expenseCategories, 'expense', progress);
        drawPie('incomePie', analysis.incomeCategories, 'income', progress);
        if (progress < 1) requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    }

    function bindTrendHover(canvasId, tipId) {
      const canvas = $(canvasId);
      canvas.addEventListener('mousemove', (event) => {
        const rect = canvas.getBoundingClientRect();
        const x = (event.clientX - rect.left) * (canvas.width / (window.devicePixelRatio || 1)) / rect.width;
        const nearest = (trendHoverPoints[canvasId] || [])
          .map(point => ({point, distance: Math.abs(point.x - x)}))
          .sort((a, b) => a.distance - b.distance)[0];
        const tip = $(tipId);
        if (!nearest || nearest.distance > 24) {
          tip.style.display = 'none';
          return;
        }
        const sameDay = (trendHoverPoints[canvasId] || []).filter(point => point.day === nearest.point.day);
        tip.innerHTML = `<strong>${esc(nearest.point.day)}</strong>` + sameDay.map(point =>
          `${esc(point.label)}：${esc(signedMoney(point.value, point.tone))}`
        ).join('<br>');
        tip.style.left = `${event.clientX - rect.left}px`;
        tip.style.top = `${event.clientY - rect.top}px`;
        tip.style.display = 'block';
      });
      canvas.addEventListener('mouseleave', () => {
        $(tipId).style.display = 'none';
      });
    }

    bindTrendHover('cashflowTrend', 'cashflowTrendTip');

    function pieHitTest(canvas, event) {
      const rect = canvas.getBoundingClientRect();
      const x = (event.clientX - rect.left) * (canvas.width / (window.devicePixelRatio || 1)) / rect.width;
      const y = (event.clientY - rect.top) * (canvas.height / (window.devicePixelRatio || 1)) / rect.height;
      return (pieHoverRegions[canvas.id] || []).find(region => {
        const dx = x - region.cx;
        const dy = y - region.cy;
        const distance = Math.hypot(dx, dy);
        if (distance < region.innerRadius || distance > region.radius) return false;
        let angle = Math.atan2(dy, dx);
        if (angle < -Math.PI / 2) angle += Math.PI * 2;
        return angle >= region.start && angle <= region.end;
      }) || null;
    }

    function bindPieHover(canvasId, tipId, tone) {
      const canvas = $(canvasId);
      canvas.addEventListener('mousemove', (event) => {
        const hit = pieHitTest(canvas, event);
        const tip = $(tipId);
        canvas.style.cursor = hit ? 'pointer' : 'default';
        if (!hit) {
          tip.style.display = 'none';
          return;
        }
        const percent = (hit.percent * 100).toFixed(1);
        tip.innerHTML = `<strong>${esc(categoryLabel(hit.row.category || '未分类'))}</strong>${esc(signedMoney(hit.row.amount_cents, hit.tone))} · ${percent}%`;
        const wrapRect = canvas.parentElement.getBoundingClientRect();
        tip.style.left = `${event.clientX - wrapRect.left}px`;
        tip.style.top = `${event.clientY - wrapRect.top}px`;
        tip.style.display = 'block';
      });
      canvas.addEventListener('mouseleave', () => {
        $(tipId).style.display = 'none';
        canvas.style.cursor = 'default';
      });
      canvas.addEventListener('click', (event) => {
        const hit = pieHitTest(canvas, event);
        if (!hit) return;
        jumpToEntriesByCategory(hit.row.category || '未分类', tone);
      });
    }

    bindPieHover('expensePie', 'expensePieTip', 'expense');
    bindPieHover('incomePie', 'incomePieTip', 'income');
    window.addEventListener('resize', () => {
      clearTimeout(chartResizeTimer);
      chartResizeTimer = setTimeout(() => {
        if (summaryData) updateOverview();
      }, 120);
    });

    function initPeriodDefaults() {
      if (periodInitialized) {
        clampCycleIndex();
        return;
      }
      const days = entries.map(entryDay).filter(Boolean).sort();
      const today = ymd(new Date());
      customStart = customStart || days[0] || today;
      customEnd = customEnd || today;
      clampCycleIndex();
      periodInitialized = true;
    }

    function weekStart(date) {
      const start = new Date(date);
      const day = start.getDay() || 7;
      start.setDate(start.getDate() - day + 1);
      return start;
    }

    function buildCycles() {
      const today = new Date();
      if (cycleMode === 'quarter') {
        const currentQuarterStartMonth = Math.floor(today.getMonth() / 3) * 3;
        const cycles = [];
        for (let i = 0; i < 40; i += 1) {
          const startDate = new Date(today.getFullYear(), currentQuarterStartMonth - i * 3, 1);
          const endDate = new Date(startDate.getFullYear(), startDate.getMonth() + 3, 0);
          const quarter = Math.floor(startDate.getMonth() / 3) + 1;
          cycles.push({label: `${startDate.getFullYear()} Q${quarter}`, start: ymd(startDate), end: ymd(endDate)});
        }
        return cycles;
      }
      if (cycleMode === 'week') {
        const cycles = [];
        const cursor = weekStart(today);
        for (let i = 0; i < 60; i += 1) {
          const start = addDays(cursor, -7 * i);
          const end = addDays(start, 6);
          cycles.push({label: `${ymd(start).replaceAll('-', '/')} - ${ymd(end).replaceAll('-', '/')}`, start: ymd(start), end: ymd(end)});
        }
        return cycles;
      }
      const cycles = [];
      for (let i = 0; i < 90; i += 1) {
        const day = ymd(addDays(today, -i));
        cycles.push({label: day.replaceAll('-', '/'), start: day, end: day});
      }
      return cycles;
    }

    function clampCycleIndex() {
      const cycles = buildCycles();
      selectedCycleIndex = Math.min(selectedCycleIndex, Math.max(cycles.length - 1, 0));
    }

    function currentRange() {
      if (periodMode === 'all') return {start: customStart, end: customEnd};
      if (periodMode === 'year') {
        const year = new Date().getFullYear() + yearOffset;
        return {start: `${year}-01-01`, end: `${year}-12-31`};
      }
      if (periodMode === 'month') {
        const now = new Date();
        const date = new Date(now.getFullYear(), now.getMonth() + monthOffset, 1);
        const month = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}`;
        return {start: `${month}-01`, end: ymd(new Date(date.getFullYear(), date.getMonth() + 1, 0))};
      }
      const cycles = buildCycles();
      return cycles[selectedCycleIndex] || {start: customStart, end: customEnd};
    }

    function entriesInRange(range) {
      return entries.filter(entry => {
        const day = entryDay(entry);
        return day && day >= range.start && day <= range.end;
      });
    }

    function daysInRange(range) {
      const start = parseDay(range.start);
      const end = parseDay(range.end);
      if (!start || !end || start > end) return [];
      const days = [];
      for (let cursor = start; cursor <= end; cursor = addDays(cursor, 1)) {
        days.push(ymd(cursor));
      }
      return days;
    }

    function aggregateRange(range) {
      const scoped = entriesInRange(range);
      const reportable = scoped.filter(reportableEntry);
      const refunds = reportable.filter(e => e.type === '退款');
      // 净额制：只有手动关联的退款才冲减支出；未关联退款不进任何收支
      const linkedRefunds = refunds.filter(e => e.refund_pair_id);
      const unlinkedRefunds = refunds.filter(e => !e.refund_pair_id);
      const refundTotal = linkedRefunds.reduce((sum, e) => sum + Number(e.amount_cents || 0), 0);
      const refundUnlinkedTotal = unlinkedRefunds.reduce((sum, e) => sum + Number(e.amount_cents || 0), 0);
      const allById = new Map(entries.map(e => [Number(e.id), e]));
      // 净额冲减流（扫全量账本）：每笔已关联退款冲减到"原支出那天/原支出分类"，
      // 退款晚于支出、跨日期甚至跨月都归位到实际支出那天（与后端 net_expense_stats 同口径）
      const refundOffsets = [];
      entries.forEach(e => {
        if (e.type !== '退款' || !e.refund_pair_id) return;
        const src = allById.get(Number(e.refund_pair_id));
        const day = src ? String(src.transaction_time || '').slice(0, 10) : '';
        refundOffsets.push({
          day,
          category: src ? (src.category || '未分类') : refundPairCategory(e),
          amount: Number(e.amount_cents || 0),
          inRange: Boolean(day) && day >= range.start && day <= range.end,
        });
      });
      const offsetsInRange = refundOffsets.filter(o => o.inRange);
      const offsetTotal = offsetsInRange.reduce((sum, o) => sum + o.amount, 0);
      const income = reportable.filter(e => increaseTypes.has(e.type)).reduce((sum, e) => sum + Number(e.amount_cents || 0), 0);
      const expense = reportable.filter(e => decreaseTypes.has(e.type)).reduce((sum, e) => sum + Number(e.amount_cents || 0), 0) - offsetTotal;
      const expenseCategoryMap = new Map();
      reportable.filter(e => decreaseTypes.has(e.type)).forEach(e => {
        const rawCategory = e.category || '未分类';
        const key = expenseCategoryLevel === 'minor' ? rawCategory : majorCategory(rawCategory);
        expenseCategoryMap.set(key, (expenseCategoryMap.get(key) || 0) + Number(e.amount_cents || 0));
      });
      // 退款按"原支出分类"冲减支出统计（只冲减原支出在区间内的部分）
      offsetsInRange.forEach(o => {
        const key = expenseCategoryLevel === 'minor' ? o.category : majorCategory(o.category);
        expenseCategoryMap.set(key, (expenseCategoryMap.get(key) || 0) - o.amount);
      });
      const incomeCategoryMap = new Map();
      reportable.filter(e => increaseTypes.has(e.type)).forEach(e => {
        const key = e.category || '未分类';
        incomeCategoryMap.set(key, (incomeCategoryMap.get(key) || 0) + Number(e.amount_cents || 0));
      });
      const byDay = new Map(daysInRange(range).map(day => [day, {day, income_cents: 0, expense_cents: 0}]));
      reportable.forEach(e => {
        const day = entryDay(e);
        if (!day) return;
        const current = byDay.get(day);
        if (!current) return;
        if (increaseTypes.has(e.type)) current.income_cents += Number(e.amount_cents || 0);
        if (decreaseTypes.has(e.type)) current.expense_cents += Number(e.amount_cents || 0);
      });
      // 已关联退款冲减到"原支出那天"（跨日期退款归位；原支出日不在区间则该笔不进本区间趋势）
      refundOffsets.forEach(o => {
        const current = byDay.get(o.day);
        if (current) current.expense_cents -= o.amount;
      });
      return {
        scoped,
        income,
        expense,
        refund: refundTotal,
        refundGross: refundTotal + refundUnlinkedTotal,
        refundUnlinkedCount: unlinkedRefunds.length,
        refundUnlinked: refundUnlinkedTotal,
        expenseCategories: [...expenseCategoryMap.entries()].filter(([, amount_cents]) => amount_cents > 0).map(([category, amount_cents]) => ({category, amount_cents})).sort((a, b) => b.amount_cents - a.amount_cents),
        incomeCategories: [...incomeCategoryMap.entries()].map(([category, amount_cents]) => ({category, amount_cents})).sort((a, b) => b.amount_cents - a.amount_cents),
        trend: [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day)),
      };
    }

    function updatePeriodVisibility() {
      $('moreControls').classList.toggle('hidden', periodMode !== 'more');
      $('prevCycle').classList.toggle('hidden', !['year', 'month', 'more'].includes(periodMode));
      $('nextCycle').classList.toggle('hidden', !['year', 'month', 'more'].includes(periodMode));
      document.querySelectorAll('[data-period-mode]').forEach(btn => btn.classList.toggle('active', btn.dataset.periodMode === periodMode));
      document.querySelectorAll('[data-cycle]').forEach(btn => btn.classList.toggle('active', btn.dataset.cycle === cycleMode));
    }

    function updateOverview() {
      if (!summaryData) return;
      clampCycleIndex();
      updatePeriodVisibility();
      document.querySelectorAll('[data-expense-category-level]').forEach(btn => btn.classList.toggle('active', btn.dataset.expenseCategoryLevel === expenseCategoryLevel));
      const range = currentRange();
      const analysis = aggregateRange(range);
      $('rangeStartText').textContent = slashDate(range.start);
      $('rangeEndText').textContent = slashDate(range.end);
      $('mIncome').textContent = signedMoney(analysis.income, 'income');
      $('mExpense').textContent = signedMoney(analysis.expense, 'expense');
      const expenseLabel = document.querySelector('#expenseKpi .label');
      if (expenseLabel) expenseLabel.textContent = analysis.refund > 0 ? '支出·净' : '支出';
      $('expenseKpi').title = analysis.refund > 0
        ? `毛支出 ${signedMoney(analysis.expense + analysis.refund, 'expense')}，已冲减退款 ${signedMoney(analysis.refund, 'income')}`
        : '支出（退款已按净额冲减）';
      $('mBalance').textContent = signedMoney(analysis.income - analysis.expense, analysis.income - analysis.expense < 0 ? 'expense' : 'income');
      const debtRows = (summaryData.account_balances || []).filter(row => isDebtKind(row));
      // 三卡分列（与资产页分组同口径）：债权 / 负债（借款+贷款）/ 信用卡（消费口径且锁定）
      const isCreditRow = row => deriveDebtType(row) === '信用卡';
      const receivableTotal = debtRows.filter(row => row.kind === 'receivable').reduce((sum, row) => sum + Math.max(debtBalanceCents(row), 0), 0);
      const creditTotal = debtRows.filter(row => row.kind === 'liability' && isCreditRow(row)).reduce((sum, row) => sum + Math.min(debtBalanceCents(row), 0), 0);
      const liabilityTotal = debtRows.filter(row => row.kind === 'liability' && !isCreditRow(row)).reduce((sum, row) => sum + Math.min(debtBalanceCents(row), 0), 0);
      const setDebtKpi = (elId, value) => {
        const el = $(elId);
        el.textContent = signedMoney(value, value < 0 ? 'expense' : 'income');
        el.className = `metric ${value < 0 ? 'expense' : 'income'}`;
      };
      setDebtKpi('mReceivable', receivableTotal);
      setDebtKpi('mLiability', liabilityTotal);
      setDebtKpi('mCredit', creditTotal);
      $('receivableKpi').title = debtRows.some(row => row.kind === 'receivable')
        ? `债权合计（别人欠我的）${signedMoney(receivableTotal, 'income')}，点击查看资产页`
        : '当前没有债权账户，可在资产页「债权」区添加';
      $('liabilityKpi').title = debtRows.some(row => row.kind === 'liability' && !isCreditRow(row))
        ? `负债合计（我欠别人的）${signedMoney(liabilityTotal, 'expense')}，点击查看资产页`
        : '当前没有借款/贷款类负债账户，可在资产页「负债」区添加';
      $('creditKpi').title = debtRows.some(row => row.kind === 'liability' && isCreditRow(row))
        ? `信用卡类欠款合计（先消费后还款）${signedMoney(creditTotal, 'expense')}，点击查看资产页`
        : '当前没有信用卡类账户（花呗/白条/信用卡等）';
      const investMarket = Number(summaryData.invest_market_cents || 0);
      const investUnrealized = summaryData.invest_unrealized_cents;
      $('mInvest').textContent = signedMoney(investMarket, investMarket < 0 ? 'expense' : 'income');
      $('investKpi').title = investUnrealized !== null && investUnrealized !== undefined
        ? `总市值 ${signedMoney(investMarket, 'income')}，浮盈 ${signedMoney(investUnrealized, investUnrealized < 0 ? 'expense' : 'income')}，点击查看资产页投资组`
        : '暂无持仓市值，点击查看资产页投资组';
      $('mPending').textContent = summaryData.unconfirmed_count;
      $('pendingNavBadge').textContent = summaryData.unconfirmed_count;
      $('pendingNavBadge').classList.toggle('hidden', Number(summaryData.unconfirmed_count || 0) === 0);
      const unlinkedRefundCount = analysis.refundUnlinkedCount || 0;
      $('mRefund').textContent = signedMoney(analysis.refund, 'income');
      const refundLabel = document.querySelector('#refundKpi .label');
      if (refundLabel) refundLabel.textContent = unlinkedRefundCount > 0 ? `退款·待关联${unlinkedRefundCount}` : '退款';
      $('refundKpi').title = unlinkedRefundCount > 0
        ? `已关联退款冲减支出 ${signedMoney(analysis.refund, 'income')}；另有 ${unlinkedRefundCount} 笔（${signedMoney(analysis.refundUnlinked, 'income')}）待关联，未计入冲减。点击处理`
        : analysis.refund > 0
          ? `退款冲减支出 ${signedMoney(analysis.refund, 'income')}，点击查看退款与原支出配对`
          : '暂无退款记录，点击进入退款视图';
      animateOverviewCharts(analysis);
      renderActivities();
      if (expandedActivityId) hydrateActivityDetail(expandedActivityId);
      // 周期范围变化也影响账单列表：同步日期筛选快照并重渲染
      const activeView = document.querySelector('nav button.active');
      if (activeView && activeView.dataset.view === 'entries') {
        entryFilters.start = range.start;
        entryFilters.end = range.end;
        renderEntries();
      }
    }

    function activePickerValue() {
      const range = currentRange();
      return activeDateTarget === 'start' ? range.start : range.end;
    }

    function renderDatePicker() {
      const yearSelect = $('datePickerYear');
      const monthSelect = $('datePickerMonth');
      const selectedValue = activePickerValue();
      const selected = selectedValue ? parseDay(selectedValue) : new Date();
      const currentYear = new Date().getFullYear();
      const minYear = Math.min(currentYear - 10, selected.getFullYear() - 5);
      const maxYear = Math.max(currentYear + 5, selected.getFullYear() + 5);
      yearSelect.innerHTML = Array.from({length: maxYear - minYear + 1}, (_, i) => minYear + i)
        .map(year => `<option value="${year}">${year}年</option>`).join('');
      monthSelect.innerHTML = Array.from({length: 12}, (_, i) => i)
        .map(month => `<option value="${month}">${month + 1}月</option>`).join('');
      yearSelect.value = String(pickerMonth.getFullYear());
      monthSelect.value = String(pickerMonth.getMonth());

      const first = new Date(pickerMonth.getFullYear(), pickerMonth.getMonth(), 1);
      const gridStart = addDays(first, -(first.getDay() || 7) + 1);
      const cells = [];
      for (let i = 0; i < 42; i += 1) {
        const day = addDays(gridStart, i);
        const value = ymd(day);
        const classes = [
          day.getMonth() !== pickerMonth.getMonth() ? 'outside' : '',
          value === selectedValue ? 'selected' : '',
        ].filter(Boolean).join(' ');
        cells.push(`<button class="${classes}" data-date="${value}">${day.getDate()}</button>`);
      }
      $('datePickerGrid').innerHTML = cells.join('');
    }

    function openDatePicker(target, trigger) {
      activeDateTarget = target;
      const value = activePickerValue();
      pickerMonth = value ? parseDay(value) : new Date();
      renderDatePicker();
      const picker = $('datePicker');
      const panelRect = $('periodPanel').getBoundingClientRect();
      const triggerRect = trigger.getBoundingClientRect();
      picker.style.left = `${Math.max(0, triggerRect.left - panelRect.left)}px`;
      picker.style.top = `${triggerRect.bottom - panelRect.top + 8}px`;
      picker.classList.remove('hidden');
    }

    function closeDatePicker() {
      $('datePicker').classList.add('hidden');
      activeDateTarget = null;
    }

    async function refresh() {
      const [summary, entryData, pending, cats, tree, accounts, accountFilters, debts, rules, activityData] = await Promise.all([
        api('/api/summary'), api('/api/entries-all'), api('/api/unconfirmed'), api('/api/categories'), api('/api/category-tree'), api('/api/account-names'), api('/api/account-filter-names'), api('/api/account-debts'), api('/api/keyword-rules'), api('/api/activities')
      ]);
      activities = Array.isArray(activityData) ? activityData : [];
      entries = pendingDelete ? entryData.filter(entry => Number(entry.id) !== Number(pendingDelete.row.id)) : entryData;
      const summaryAccounts = summary.account_balances || summary.accounts || [];
      accountBalances = pendingAccountDelete ? summaryAccounts.filter(row => row.name !== pendingAccountDelete.row.name) : summaryAccounts;
      debtBalances = pendingDebtDelete ? debts.filter(row => Number(row.id) !== Number(pendingDebtDelete.row.id)) : debts;
      pendingItems = orderPendingItems(pending.map(item => ({
        ...item,
        ui_id: `${item.source || 'unconfirmed'}:${item.id}`
      })));
      categoryOptions = cats;
      categoryTree = tree;
      entryTypes = Object.keys(categoryTree);
      // 负债账户同步进「还款」分类：净额制下还款流水的缓冲账户=负债账户，
      // 分类里直接列真实负债账户（花呗、借款-XX…），替代静态的"花呗还款/白条还款"
      const liveLiabilityNames = accountBalances
        .filter(row => row.kind === 'liability' && Number(row.archived || 0) !== 1)
        .map(row => row.name);
      if (liveLiabilityNames.length) {
        const merged = {};
        liveLiabilityNames.forEach(name => { merged[name] = []; });
        Object.entries(categoryTree['还款'] || {}).forEach(([major, leaves]) => {
          if (!(major in merged)) merged[major] = leaves;
        });
        categoryTree['还款'] = merged;
        liveLiabilityNames.forEach(name => { if (!categoryOptions.includes(name)) categoryOptions.push(name); });
      }
      if (entryFilters.type && !entryTypes.includes(entryFilters.type)) {
        entryFilters.type = '';
        entryFilters.category = '';
      }
      if (entryFilters.category && !categoryOptions.includes(entryFilters.category)) entryFilters.category = '';
      if (entryFilters.type && entryFilters.category && !categoriesForType(entryFilters.type).includes(entryFilters.category)) entryFilters.category = '';
      accountOptions = accounts;
      accountFilterOptions = accountFilters;
      keywordRules = rules;
      summaryData = summary;
      initPeriodDefaults();
      renderAssetManager();
      renderQuickFormOptions();
      if (!$('fTime').value) $('fTime').value = nowDatetimeLocal();
      renderEntries();
      updateOverview();
      renderPending();
      renderCategoryRules();
    }

    // ---------- 活动（活动）：与分类平行的第二视角 ----------
    function renderActivities() {
      const list = $('activityList');
      if (!list) return;
      // 卡片渲染（活动列表与归档折叠区共用）
      const activityCardHtml = a => {
          // 时间跨度一律跟随挂载账单的实际起止，手动存的时间仅作无账单时的兜底
          const span = (a.first_time || a.last_time)
            ? `${String(a.first_time).slice(0, 10)} ~ ${String(a.last_time).slice(0, 10)}`
            : (a.start_date || a.end_date ? `${a.start_date || '…'} ~ ${a.end_date || '…'}` : '暂无账单');
          const expanded = expandedActivityId === Number(a.id);
          const renaming = renamingActivityId === Number(a.id);
          const isArchived = Number(a.archived) === 1;
          const refundTag = Number(a.refund_cents) > 0
            ? `<span class="activity-refund-tag" title="毛支出 ${signedMoney(a.gross_cents, 'expense')}，退款冲减 ${signedMoney(a.refund_cents, 'income')}">含冲减</span>`
            : '';
          // 行内改名：标题原位变输入框，操作区变 ✓/✕；改名时禁止点展开避免状态混乱
          const head = renaming ? `
              <div class="activity-title">
                <input class="activity-rename-input" data-activity-rename-input="${a.id}" value="${esc(a.name)}" maxlength="50" placeholder="活动名称">
              </div>
              <div class="activity-money">${signedMoney(a.net_cents, 'expense')}</div>
              <div class="activity-actions">
                <button class="primary" data-activity-rename-save="${a.id}">保存</button>
                <button class="secondary" data-activity-rename-cancel>取消</button>
              </div>` : `
              <div class="activity-title" data-activity-toggle="${a.id}" title="点击展开/收起明细">
                <strong>${esc(a.name)}</strong>
                <span class="muted">${esc(span)} · ${a.expense_count} 笔</span>${refundTag}
                ${isArchived ? '<span class="activity-archived-tag">已归档</span>' : ''}
              </div>
              <div class="activity-money">${signedMoney(a.net_cents, 'expense')}</div>
              <div class="activity-actions">
                <button class="secondary" data-activity-toggle="${a.id}">${expanded ? '收起' : '明细'}</button>
                ${isArchived
                  ? `<button class="secondary" data-activity-restore="${a.id}" title="恢复到活动列表，可继续添加账单">恢复</button>`
                  : `<button class="secondary" data-activity-edit="${a.id}">编辑</button>
                     <button class="secondary" data-activity-archive="${a.id}" title="归档后不再出现在账单页「加入活动」列表，数据保留可随时恢复">归档</button>
                     <button class="secondary" data-activity-delete="${a.id}">删除</button>`}
              </div>`;
          return `
            <div class="activity-card${expanded ? ' expanded' : ''}${isArchived ? ' is-archived' : ''}">
              <div class="activity-head">${head}</div>
              ${expanded ? `<div class="activity-detail" id="activityDetail-${a.id}">加载中…</div>` : ''}
            </div>`;
      };
      const activeList = activities.filter(a => Number(a.archived) !== 1);
      const archivedList = activities.filter(a => Number(a.archived) === 1);
      if (!activities.length) {
        list.innerHTML = '<div class="muted activity-empty">还没有活动。点右上角「＋ 新建活动」创建一个（如「10月旅游」），然后到账单页勾选相关支出、点「加入活动」。</div>';
      } else {
        list.innerHTML = activeList.map(activityCardHtml).join('');
        // 归档折叠区：已归档的不混在活动列表里，收进底部折叠条（大众归档习惯）
        if (archivedList.length) {
          list.insertAdjacentHTML('beforeend', `
            <button class="activity-archived-toggle" id="archivedToggle">
              已归档活动（${archivedList.length}）<span class="archived-arrow">${showArchived ? '▴' : '▾'}</span>
            </button>
            <div class="activity-archived-area ${showArchived ? '' : 'hidden'}" id="archivedArea">
              ${showArchived ? archivedList.map(activityCardHtml).join('') : ''}
            </div>`);
        }
      }
      // 表单只做新建：弹出时右上按钮隐藏，保存/取消后表单消失、按钮回来
      const form = $('activityForm');
      form.classList.toggle('hidden', activityFormMode !== 'create');
      $('activityAddBtn').classList.toggle('hidden', activityFormMode === 'create');
      if (activityFormMode === 'create') {
        $('activityName').value = '';
      }
      list.querySelectorAll('[data-activity-toggle]').forEach(el => el.onclick = () => {
        const id = Number(el.dataset.activityToggle);
        expandedActivityId = expandedActivityId === id ? null : id;
        renderActivities();
        if (expandedActivityId === id) hydrateActivityDetail(id);
      });
      list.querySelectorAll('[data-activity-edit]').forEach(btn => btn.onclick = () => {
        renamingActivityId = Number(btn.dataset.activityEdit);
        renderActivities();
        const input = list.querySelector(`[data-activity-rename-input="${renamingActivityId}"]`);
        if (input) { input.focus(); input.select(); }
      });
      const saveRename = async (id, input) => {
        const name = input.value.trim();
        if (!name) { input.focus(); $('status').textContent = '活动名称不能为空'; return; }
        try {
          await api('/api/activity-update', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({id, name})});
          renamingActivityId = null;
          await refresh();
        } catch (err) { $('status').textContent = err.message; }
      };
      list.querySelectorAll('[data-activity-rename-save]').forEach(btn => btn.onclick = () => {
        const id = Number(btn.dataset.activityRenameSave);
        saveRename(id, list.querySelector(`[data-activity-rename-input="${id}"]`));
      });
      list.querySelectorAll('[data-activity-rename-cancel]').forEach(btn => btn.onclick = () => {
        renamingActivityId = null;
        renderActivities();
      });
      list.querySelectorAll('[data-activity-rename-input]').forEach(input => {
        input.onkeydown = event => {
          if (event.key === 'Enter') {
            event.preventDefault();
            saveRename(Number(input.dataset.activityRenameInput), input);
          } else if (event.key === 'Escape') {
            renamingActivityId = null;
            renderActivities();
          }
        };
      });
      // 归档 / 恢复：账单数据保留，只控制活动是否出现在账单页「加入活动」列表
      list.querySelectorAll('[data-activity-archive]').forEach(btn => btn.onclick = async () => {
        const id = Number(btn.dataset.activityArchive);
        const act = activities.find(x => Number(x.id) === id);
        if (!confirm(`归档活动「${act ? act.name : id}」？\n归档后：账单页「加入活动」不再显示它，已有账单和数据保留，可随时在下方「已归档活动」里恢复。`)) return;
        try {
          await api('/api/activity-archive', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({id, archived: true})});
          if (expandedActivityId === id) expandedActivityId = null;
          await refresh();
        } catch (err) { $('status').textContent = err.message; }
      });
      list.querySelectorAll('[data-activity-restore]').forEach(btn => btn.onclick = async () => {
        const id = Number(btn.dataset.activityRestore);
        if (expandedActivityId === id) expandedActivityId = null; // 恢复后回列表收起状态，避免"展开中却无内容"
        try {
          await api('/api/activity-archive', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({id, archived: false})});
          await refresh();
        } catch (err) { $('status').textContent = err.message; }
      });
      const archivedToggle = $('archivedToggle');
      if (archivedToggle) archivedToggle.onclick = () => { showArchived = !showArchived; renderActivities(); };
      list.querySelectorAll('[data-activity-delete]').forEach(btn => btn.onclick = async () => {
        const id = Number(btn.dataset.activityDelete);
        const act = activities.find(x => Number(x.id) === id);
        if (!confirm(`删除活动「${act ? act.name : id}」？\n账单会保留，只是摘出该活动。`)) return;
        try {
          await api('/api/activity-delete', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({id})});
          if (expandedActivityId === id) expandedActivityId = null;
          await refresh();
        } catch (err) { $('status').textContent = err.message; }
      });
    }

    async function hydrateActivityDetail(aid) {
      const box = $(`activityDetail-${aid}`);
      if (!box) return;
      try {
        const data = await api(`/api/activity-entries?id=${aid}`);
        const act = activities.find(x => Number(x.id) === Number(aid));
        // 归档即封存：归档活动的明细只读（无移出按钮），恢复后才能改
        const actArchived = act && Number(act.archived) === 1;
        const expRows = (data.expenses || []).map(e => `
          <div class="activity-row">
            <span class="activity-time">${esc(String(e.transaction_time || '').slice(0, 16))}</span>
            <span class="activity-cat">${esc(categoryLabel(e.category) || '未分类')}</span>
            <span class="activity-obj">${esc(e.transaction_object || '')}</span>
            <span class="activity-amt expense">−${plainMoney(Number(e.amount_cents))}</span>
            ${actArchived ? '' : `<button class="activity-row-remove" data-activity-remove="${e.id}" title="从该活动移出此账单（账单本身保留）">移出</button>`}
          </div>`).join('');
        const refundRows = (data.refunds || []).map(r => {
          const src = (data.expenses || []).find(e => Number(e.id) === Number(r.refund_pair_id));
          return `
          <div class="activity-row is-refund">
            <span class="activity-time">${esc(String(r.transaction_time || '').slice(0, 16))}</span>
            <span class="activity-cat">退款冲减</span>
            <span class="activity-obj">原支出：${esc(src ? (src.transaction_object || categoryLabel(src.category) || '') : `#${r.refund_pair_id}`)}</span>
            <span class="activity-amt income">+${plainMoney(Number(r.amount_cents))}</span>
          </div>`;
        }).join('');
        box.innerHTML = (expRows || refundRows)
          ? `<div class="activity-rows">${expRows}${refundRows}</div>
             <div class="activity-sum muted">毛支出 ${signedMoney(act ? act.gross_cents : 0, 'expense')} · 退款冲减 ${signedMoney(act ? act.refund_cents : 0, 'income')} · 净额 ${signedMoney(act ? act.net_cents : 0, 'expense')}</div>`
          : '<div class="muted">该活动还没有挂任何支出。到账单页勾选相关支出，点上方「加入活动」。</div>';
        // 每行「移出」：账单保留，只摘出该活动（净额/合计随之重算）
        box.querySelectorAll('[data-activity-remove]').forEach(btn => btn.onclick = async () => {
          if (!confirm('把这笔账单移出该活动？\n账单本身保留，只解除挂载关系。')) return;
          try {
            await api('/api/entry-activity', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({ids: [Number(btn.dataset.activityRemove)], activity_id: null})});
            await refresh();
          } catch (err) { $('status').textContent = err.message; }
        });
      } catch (err) {
        box.innerHTML = `<div class="muted">加载失败：${esc(err.message)}</div>`;
      }
    }

    async function saveActivityForm() {
      const name = $('activityName').value.trim();
      if (!name) { $('status').textContent = '活动名称不能为空'; return; }
      // 时间跨度完全由挂载账单的实际起止决定，不再手动选择
      try {
        await api('/api/activity-add', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({name})});
        activityFormMode = null;
        await refresh();
      } catch (err) { $('status').textContent = err.message; }
    }

    async function applyBatchActivity(activityId) {
      const valid = new Set(entries.filter(e => e.type === '支出').map(e => Number(e.id)));
      const ids = [...checkedEntries].filter(id => valid.has(Number(id)));
      if (!ids.length) { $('status').textContent = '请先勾选要挂载的支出账单'; return; }
      try {
        await api('/api/entry-activity', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({ids, activity_id: activityId || null})});
        checkedEntries.clear();
        await refresh();
      } catch (err) { $('status').textContent = err.message; }
    }

    function renderEntries() {
      rebuildRefundLinks();
      // 清理已不存在的勾选（被删除的账单）
      const validIds = new Set(entries.map(e => Number(e.id)));
      checkedEntries = new Set([...checkedEntries].filter(id => validIds.has(id)));
      const q = $('search').value.trim();
      const searched = q ? entries.filter(e => JSON.stringify(e).includes(q)) : entries;
      const filtered = searched.filter(entryMatchesFilters);
      // 所有视图统一按"归属时间"排序：已关联退款归属到原支出的时间（净额口径），
      // 否则退款行按自身时间排在 10-05，日期组头却按归属日 10-04 弹出，造成日期组交错
      filtered.sort((a, b) => {
        const ta = entryAnchorTime(a);
        const tb = entryAnchorTime(b);
        if (ta !== tb) return tb.localeCompare(ta);
        // 同一时间：原支出在前、其退款紧随其后成对显示
        return ((a.type === '退款' ? 1 : 0) - (b.type === '退款' ? 1 : 0)) || (Number(b.id) || 0) - (Number(a.id) || 0);
      });
      const pageSize = entryPageSize === 'all' ? filtered.length || 1 : Number(entryPageSize);
      const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
      entryPage = Math.min(Math.max(1, entryPage), totalPages);
      const start = (entryPage - 1) * pageSize;
      const pageRows = filtered.slice(start, start + pageSize);
      const shownStart = filtered.length ? start + 1 : 0;
      const shownEnd = Math.min(start + pageSize, filtered.length);
      const pageSizeOptions = ['10', '20', '50', '100', '200', '500', 'all'];
      const activityOptions = activities.filter(a => Number(a.archived) !== 1).map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
      const batchBar = `
        <div class="entry-batchbar">
          <span class="muted">勾选支出行，把相关账单挂进同一个活动（如旅游、约会）</span>
          <select id="batchActivitySelect"><option value="">选择活动…</option>${activityOptions}</select>
          <button class="primary" id="batchAssignBtn" ${checkedEntries.size && activities.length ? '' : 'disabled'}>加入活动</button>
          <button class="secondary" id="batchUnassignBtn" ${checkedEntries.size ? '' : 'disabled'}>移出活动</button>
          ${checkedEntries.size ? `<span class="muted">已选 ${checkedEntries.size} 笔</span>` : ''}
        </div>`;
      const controls = `
        <div class="entry-controls">
          <div class="muted">共 ${filtered.length} 条，当前 ${shownStart}-${shownEnd} 条${entryFilterSummary() ? ` · ${esc(entryFilterSummary())}` : ''}</div>
          <div class="entry-pager">
            <label>每页
              <select id="entryPageSize">
                ${pageSizeOptions.map(value => `<option value="${value}" ${value === entryPageSize ? 'selected' : ''}>${value === 'all' ? '全部' : value}</option>`).join('')}
              </select>
            </label>
            <button class="secondary" id="entryPrev" ${entryPage <= 1 ? 'disabled' : ''}>上一页</button>
            <span>${entryPage} / ${totalPages}</span>
            <button class="secondary" id="entryNext" ${entryPage >= totalPages ? 'disabled' : ''}>下一页</button>
          </div>
        </div>${batchBar}`;
      // 跨日冲减标注：退款实际发生在另一天，但净额归属原支出那天
      const timeCellWithAnchor = (value, row) => {
        if (row.type === '退款' && row.refund_pair_id) {
          const ownDay = String(row.transaction_time || '').slice(0, 10);
          const anchorDay = entryAnchorDay(row);
          if (ownDay && anchorDay && ownDay !== anchorDay) {
            const [ay, am, ad] = anchorDay.split('-');
            return {text: value, html: `${esc(value)}<br><span class="muted" style="font-size:11px">冲减${Number(am)}月${Number(ad)}日支出</span>`};
          }
        }
        return value;
      };
      // 状态列已移除：entries 全部为 confirmed（待确认条目在「待确认」页处理），状态无信息量
      const sourceCell = value => value ? value : {text: '', html: '<span class="muted">—</span>'};
      const columns = [['transaction_time','时间', timeCellWithAnchor], ['type','类型', typeCellWithRefund], ['category','分类', categoryLabel], ['amount_cents','金额', amountCellWithNet, moneyClass], ['bill_source','来源', sourceCell], ['account','账户'], ['transaction_object','交易对象'], ['note','备注']];
      const sourceFilterOptions = () => [...new Set(entries.map(e => (e.bill_source || '').trim()).filter(Boolean))].concat(['(无来源)']);
      const headerCells = [
        '<th class="entry-check-head"><input type="checkbox" id="entryCheckAll" title="全选本页支出"></th>',
        dateHeaderCell(),
        headerFilterCell('type', '类型', entryTypes),
        headerFilterCell('category', '分类', entryFilterCategoryOptions()),
        '<th><div class="entry-head-filter static"><span class="entry-head-label">金额</span></div></th>',
        headerFilterCell('bill_source', '来源', sourceFilterOptions()),
        headerFilterCell('account', '账户', accountFilterOptions),
        '<th><div class="entry-head-filter static"><span class="entry-head-label">交易对象</span></div></th>',
        '<th><div class="entry-head-filter static"><span class="entry-head-label">备注</span></div></th>',
        '<th class="entry-actions-head"></th>',
      ];
      const header = '<table class="entry-table"><thead><tr>' + headerCells.join('') + '</tr></thead><tbody>';
      // 按日分组：日期组头显示当日支出总额（对全部过滤结果统计，跨页也准确）
      // 已关联退款归位到"原支出那天"（entryAnchorDay），与排序、日小计同一口径
      const dayKeyOf = entryAnchorDay;
      // 日小计 = 当日净支出：毛支出 − 已关联退款冲减（与 /api/summary 趋势同口径）
      // 冲减从支出行上的关联退款取，不依赖退款行自身通过筛选：
      // 类型筛选/支出语气视图会隐藏退款行，若按 filtered 里的退款行冲减会丢失（显示回毛支出）
      const dayTotals = new Map();
      const dayRefunds = new Map();
      filtered.forEach(e => {
        const key = dayKeyOf(e);
        if (!key) return;
        if (e.type === '支出') {
          dayTotals.set(key, (dayTotals.get(key) || 0) + Number(e.amount_cents || 0));
          const links = linkedRefundsOf(e);
          if (links.length) {
            const ded = links.reduce((s, r) => s + Number(r.amount_cents || 0), 0);
            dayTotals.set(key, dayTotals.get(key) - ded);
            dayRefunds.set(key, (dayRefunds.get(key) || 0) + ded);
          }
        }
      });
      const weekdayNames = ['日', '一', '二', '三', '四', '五', '六'];
      const dayHeaderHtml = key => {
        const [y, m, d] = key.split('-').map(Number);
        const wd = weekdayNames[new Date(y, m - 1, d).getDay()];
        const refundPart = dayRefunds.get(key) || 0;
        const refundNote = refundPart ? `<span class="muted">（已冲减退款 ${plainMoney(refundPart)}）</span>` : '';
        return `<tr class="entry-day-row"><td colspan="${columns.length + 2}"><div class="entry-day-inner"><span class="entry-day-label">${m}月${d}日 星期${wd}</span><span class="entry-day-total">支出：${plainMoney(dayTotals.get(key) || 0)}${refundNote}</span></div></td></tr>`;
      };
      let lastDayKey = null;
      const renderEntryRow = row => {
        const isExpense = row.type === '支出';
        const checkCell = `<td class="entry-check-cell"><input type="checkbox" data-entry-check="${row.id}" ${checkedEntries.has(Number(row.id)) ? 'checked' : ''} ${isExpense ? '' : 'disabled title="只有支出能挂活动"'}></td>`;
        const cells = columns.map(c => {
          const formatted = c[2] ? c[2](row[c[0]], row) : row[c[0]];
          const isHtml = formatted && typeof formatted === 'object';
          const value = isHtml ? formatted.text : formatted;
          const classes = ['entry-cell'];
          if (c[3]) classes.push(c[3](row[c[0]], row));
          const body = isHtml ? formatted.html : esc(value);
          return `<td class="${esc(classes.filter(Boolean).join(' '))}" title="${esc(value)}"><span class="entry-cell-text">${body}</span></td>`;
        }).join('');
        const editRow = editingEntryId === row.id ? `
          <tr class="entry-edit-row">
            <td colspan="${columns.length + 2}">
              <div class="entry-edit-panel" data-entry-form="${row.id}">
                <label>时间<input data-field="transaction_time" type="datetime-local" value="${esc(datetimeInputValue(row.transaction_time))}"></label>
                <label>类型<select data-field="type" data-entry-type="${row.id}">${optionsHtml(entryTypes, row.type)}</select></label>
                <label>分类<input class="cat-search" data-field="category" data-entry-category="${row.id}" data-cat-type="${esc(row.type)}" value="${esc(row.category || '')}" placeholder="点击选择或搜索分类" autocomplete="off"></label>
                <label class="entry-edit-amount">金额<input data-field="amount" type="number" step="0.01" inputmode="decimal" value="${amountValue(row.amount_cents)}"></label>
                <label>账户<select data-field="account">${optionsHtml(accountOptions, row.account)}</select></label>
                <label>来源<input data-field="bill_source" value="${esc(row.bill_source || '')}" placeholder="微信/支付宝/银行名，可留空" title="这笔账单的流水来源渠道"></label>
                <label class="entry-target-account ${row.type === '转账' ? '' : 'hidden'}" data-entry-target-wrap="${row.id}">转入账户<select data-field="target_account">${optionsHtml(accountOptions, row.target_account, true)}</select></label>
                <label>交易对象<input data-field="transaction_object" value="${esc(row.transaction_object)}"></label>
                ${isExpense ? `<label>活动<select data-field="activity_id"><option value="">不挂活动</option>${(() => {
                  // 已归档活动不出现在可选项里；但账单已挂在归档活动上时保留显示（标注"已归档"）
                  const currentArchived = activities.find(a => Number(a.id) === Number(row.activity_id) && Number(a.archived) === 1);
                  const currentOpt = currentArchived ? `<option value="${currentArchived.id}" selected>${esc(currentArchived.name)}（已归档）</option>` : '';
                  const rest = activities.filter(a => Number(a.archived) !== 1 && Number(a.id) !== Number(row.activity_id))
                    .map(a => `<option value="${a.id}" ${Number(row.activity_id) === Number(a.id) ? 'selected' : ''}>${esc(a.name)}</option>`).join('');
                  return currentOpt + rest;
                })()}</select></label>` : ''}
                <label class="entry-edit-note">备注<input data-field="note" value="${esc(row.note)}"></label>
                <div class="entry-edit-actions">
                  <button class="primary" data-entry-save="${row.id}">保存</button>
                  <button class="secondary" data-entry-cancel>取消</button>
                </div>
              </div>
            </td>
          </tr>` : '';
        const linkRow = linkPickerRefundId === row.id ? `
          <tr class="entry-edit-row">
            <td colspan="${columns.length + 2}">
              <div class="entry-edit-panel" data-refund-picker="${row.id}">
                <div class="muted">为退款 ${plainMoney(row.amount_cents)} 元（${esc(row.transaction_time)} · ${esc(row.transaction_object || '')}）选择要关联的原支出：</div>
                <div class="refund-candidates" id="refundCandidates-${row.id}">加载候选支出中…</div>
                <div class="entry-edit-actions">
                  <button class="secondary" data-refund-cancel>取消</button>
                </div>
              </div>
            </td>
          </tr>` : '';
        const refundActions = row.type === '退款'
          ? (row.refund_pair_id
              ? `<button data-refund-unlink="${row.id}" title="解除关联后退款不再冲减支出">解除关联</button>`
              : `<button data-refund-link-pick="${row.id}" title="手动关联原支出，统计按净额冲减">关联</button>`)
          : '';
        return `
          <tr class="entry-row">
            ${checkCell}
            ${cells}
            <td class="entry-actions">
              <button data-entry-edit="${row.id}">编辑</button>
              <button data-entry-delete="${row.id}">删除</button>
              ${refundActions}
            </td>
          </tr>${editRow}${linkRow}`;
      };
      const rowsHtml = pageRows.map(row => {
        const dayKey = dayKeyOf(row);
        const dayHeader = dayKey !== lastDayKey ? (lastDayKey = dayKey, dayHeaderHtml(dayKey)) : '';
        return dayHeader + renderEntryRow(row);
      }).join('');
      const emptyRow = `<tr><td class="entry-empty" colspan="${columns.length + 2}">暂无数据</td></tr>`;
      const body = header + (pageRows.length ? rowsHtml : emptyRow) + '</tbody></table>';
      $('entryTable').innerHTML = body + controls;
      document.querySelectorAll('[data-entry-filter]').forEach(select => select.onchange = () => {
        const field = select.dataset.entryFilter;
        entryFilters[field] = select.value;
        if (field === 'year' && !entryFilters.year) {
          entryFilters.month = '';
          entryFilters.day = '';
        }
        if (field === 'month') {
          entryFilters.day = '';
        }
        if (field === 'type') {
          const scopedCategories = entryFilterCategoryOptions();
          if (entryFilters.category && !scopedCategories.includes(entryFilters.category)) entryFilters.category = '';
        }
        if (['year', 'month', 'day'].includes(field)) {
          syncEntryDateRange();
        }
        entryPage = 1;
        renderEntries();
      });
      const headerClear = $('headerClearFilters');
      if (headerClear) {
        const entriesView = $('entries');
        const canShow = entriesView && !entriesView.classList.contains('hidden');
        headerClear.style.display = canShow ? '' : 'none';
        headerClear.onclick = () => { clearEntryFilters(); entryPage = 1; renderEntries(); };
      }
      $('entryPageSize').onchange = () => {
        entryPageSize = $('entryPageSize').value;
        localStorage.setItem('bookkeeping.entryPageSize', entryPageSize);
        entryPage = 1;
        renderEntries();
      };
      $('entryPrev').onclick = () => {
        entryPage -= 1;
        renderEntries();
      };
      $('entryNext').onclick = () => {
        entryPage += 1;
        renderEntries();
      };
      // 勾选挂活动
      const checkAll = $('entryCheckAll');
      if (checkAll) {
        const pageExpenseIds = pageRows.filter(r => r.type === '支出').map(r => Number(r.id));
        checkAll.checked = pageExpenseIds.length > 0 && pageExpenseIds.every(id => checkedEntries.has(id));
        checkAll.onchange = () => {
          pageExpenseIds.forEach(id => checkAll.checked ? checkedEntries.add(id) : checkedEntries.delete(id));
          renderEntries();
        };
      }
      document.querySelectorAll('[data-entry-check]').forEach(cb => cb.onchange = () => {
        const id = Number(cb.dataset.entryCheck);
        if (cb.checked) checkedEntries.add(id); else checkedEntries.delete(id);
        renderEntries();
      });
      const batchSelect = $('batchActivitySelect');
      if (batchSelect) {
        $('batchAssignBtn').onclick = () => {
          if (!batchSelect.value) { $('status').textContent = '请先在下拉框选择要加入的活动'; return; }
          applyBatchActivity(Number(batchSelect.value));
        };
        $('batchUnassignBtn').onclick = () => applyBatchActivity(null);
      }
      document.querySelectorAll('[data-entry-edit]').forEach(btn => btn.onclick = () => {
        editingEntryId = Number(btn.dataset.entryEdit);
        renderEntries();
      });
      document.querySelectorAll('[data-entry-cancel]').forEach(btn => btn.onclick = () => {
        editingEntryId = null;
        renderEntries();
      });
      document.querySelectorAll('[data-entry-delete]').forEach(btn => btn.onclick = () => scheduleEntryDelete(Number(btn.dataset.entryDelete)));
      document.querySelectorAll('[data-refund-link-pick]').forEach(btn => btn.onclick = () => {
        linkPickerRefundId = Number(btn.dataset.refundLinkPick);
        renderEntries();
        hydrateRefundCandidates(linkPickerRefundId);
      });
      document.querySelectorAll('[data-refund-unlink]').forEach(btn => btn.onclick = async () => {
        try {
          await api('/api/refund-unlink', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({refund_id: Number(btn.dataset.refundUnlink)})});
          await refresh();
        } catch (err) { alert(err.message); }
      });
      document.querySelectorAll('[data-refund-cancel]').forEach(btn => btn.onclick = () => {
        linkPickerRefundId = null;
        renderEntries();
      });
      document.querySelectorAll('[data-entry-type]').forEach(select => select.onchange = () => {
        const categoryInput = document.querySelector(`[data-entry-category="${select.dataset.entryType}"]`);
        if (categoryInput) {
          categoryInput.value = '';
          categoryInput.dataset.catType = select.value;
          if (catDropdownInput === categoryInput) closeCategoryDropdown();
        }
        const targetWrap = document.querySelector(`[data-entry-target-wrap="${select.dataset.entryType}"]`);
        if (targetWrap) targetWrap.classList.toggle('hidden', select.value !== '转账');
      });
      document.querySelectorAll('[data-entry-save]').forEach(btn => btn.onclick = async () => {
        const id = Number(btn.dataset.entrySave);
        const form = document.querySelector(`[data-entry-form="${id}"]`);
        const payload = {id};
        form.querySelectorAll('[data-field]').forEach(input => payload[input.dataset.field] = input.value.trim());
        if (!payload.category && !['转账', '余额修正'].includes(payload.type)) {
          $('status').textContent = `类型「${payload.type}」需要选择分类（可选：${categoriesForType(payload.type).join('、') || '无'}），请在分类框中从列表选择后再保存`;
          return;
        }
        if (payload.category && !categoriesForType(payload.type).includes(payload.category)) {
          $('status').textContent = `分类「${categoryLabel(payload.category)}」不属于类型「${payload.type}」，请从列表中选择`;
          return;
        }
        try {
          await api('/api/entry-update', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify(payload)
          });
          editingEntryId = null;
          await refresh();
        } catch (err) {
          alert(err.message === 'not found' ? '后端接口未加载，请重启 Dashboard 后再保存。' : err.message);
        }
      });
    }

    // 加载退款关联候选支出列表（账单页行内面板）
    async function hydrateRefundCandidates(refundId) {
      const box = $(`refundCandidates-${refundId}`);
      if (!box) return;
      const refund = entries.find(e => Number(e.id) === Number(refundId));
      if (!refund) { box.innerHTML = '<div class="muted">退款记录不存在</div>'; return; }
      try {
        const params = new URLSearchParams({merchant: refund.transaction_object || '', time: refund.transaction_time || '', amount: refund.amount_cents || '', account: refund.account || ''});
        const data = await api(`/api/refund-candidates?${params}`);
        const seen = new Set();
        const renderGroup = list => (list || []).filter(c => !seen.has(c.id) && (seen.add(c.id), true)).map(c => {
          const linkedTag = c.linked_refund_count > 0 ? ` · 已被${c.linked_refund_count}笔退款关联` : '';
          const label = `¥${plainMoney(c.amount_cents)} · ${esc(c.transaction_time)} · ${esc(c.category || '未分类')} · ${esc(c.transaction_object || '')}${linkedTag}`;
          return `<button class="secondary refund-candidate" data-refund-candidate="${c.id}" title="${label}">${label}</button>`;
        }).join('');
        const matchHtml = renderGroup(data.matches);
        const recentHtml = renderGroup(data.recent);
        box.innerHTML = (matchHtml || recentHtml)
          ? `<div class="refund-candidate-group">${matchHtml ? `<div class="muted">按匹配度排序（金额相同 &gt; 同商户 &gt; 同账户）</div>${matchHtml}` : ''}${recentHtml ? `<div class="muted">最近支出（退款之前）</div>${recentHtml}` : ''}</div>`
          : '<div class="muted">没有找到候选支出，请确认原支出已导入</div>';
        box.querySelectorAll('[data-refund-candidate]').forEach(btn => btn.onclick = async () => {
          try {
            await api('/api/refund-link', {
              method: 'POST',
              headers: {'content-type': 'application/json'},
              body: JSON.stringify({refund_id: Number(refundId), expense_id: Number(btn.dataset.refundCandidate)})
            });
            linkPickerRefundId = null;
            await refresh();
          } catch (err) { alert(err.message); }
        });
      } catch (err) {
        box.innerHTML = `<div class="muted">候选支出加载失败：${esc(err.message)}</div>`;
      }
    }

    function categoryPath(group, leaf) {
      return leaf ? `${group}/${leaf}` : group;
    }

    function categoriesForType(type) {
      const groups = categoryTree[type] || {};
      return Object.entries(groups).flatMap(([group, leaves]) => {
        if (!leaves.length) return [group];
        return leaves.map(leaf => categoryPath(group, leaf));
      });
    }

    // 分类规则管理的类型集合：退款不参与分类（分类跟随关联的原支出），不显示不可管理
    const manageableTypes = () => entryTypes.filter(type => type !== '退款');
    function categoryRuleRows() {
      return manageableTypes().flatMap(type => categoriesForType(type).map(category => ({type, category})));
    }

    async function saveCategoryRename(input, key) {
      if (input.dataset.saving === 'true') return;
      const [type, oldCategory] = key.split('|||');
      const newCategory = input.value.trim();
      editingCategoryKey = null;
      if (!newCategory || newCategory === oldCategory) {
        renderCategoryRules();
        return;
      }
      input.dataset.saving = 'true';
      await api('/api/category-rename', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({type, old_category: oldCategory, new_category: newCategory})
      });
      await refresh();
    }

    async function saveKeywordAdd(input, key) {
      if (input.dataset.saving === 'true') return;
      const [type, category] = key.split('|||');
      const keyword = input.value.trim();
      addingKeywordKey = null;
      if (!keyword) {
        renderCategoryRules();
        return;
      }
      input.dataset.saving = 'true';
      const result = await api('/api/keyword-rule-add', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({type, category, keyword})
      });
      if (result.applied_unconfirmed) {
        $('status').textContent = `已自动归类 ${result.applied_unconfirmed} 条待确认记录`;
      }
      await refresh();
    }

    function renderCategoryRules() {
      if (!$('categoryRules')) return;
      const types = manageableTypes();
      const typeValue = types.includes($('categoryRuleType').value) ? $('categoryRuleType').value : (types.includes('支出') ? '支出' : types[0]);
      $('categoryRuleType').innerHTML = optionsHtml(types, typeValue);
      if (typeValue) $('categoryRuleType').value = typeValue;
      const rows = categoryRuleRows().filter(row =>
        !pendingCategoryDelete || row.type !== pendingCategoryDelete.type || row.category !== pendingCategoryDelete.category
      );
      const firstTypeRows = new Set();
      $('categoryRules').innerHTML = rows.length ? rows.map(row => {
        const rules = keywordRules.filter(rule => rule.category === row.category && (!rule.type || rule.type === row.type));
        const key = `${row.type}|||${row.category}`;
        const isEditing = editingCategoryKey === key;
        const isAddingKeyword = addingKeywordKey === key;
        const showType = !firstTypeRows.has(row.type);
        firstTypeRows.add(row.type);
        return `
          <section class="category-rule-row">
            <div class="category-rule-main">
              <strong>${showType ? esc(row.type) : ''}</strong>
              ${isEditing
                ? `<input class="category-inline-input" value="${esc(row.category)}" aria-label="分类名称" data-category-edit-input="${esc(key)}">`
                : `<span class="category-rule-name">${esc(categoryLabel(row.category))}</span>`}
              <button type="button" class="icon-button" title="编辑分类" aria-label="编辑分类" data-category-edit="${esc(key)}">✎</button>
            </div>
            <div class="keyword-rule-chips">
              ${rules.length ? rules.map(rule => `
                <span class="keyword-rule-chip">
                  ${esc(rule.keyword)}
                  <button type="button" aria-label="删除 ${esc(rule.keyword)}" data-rule-delete="${esc(rule.id)}">×</button>
                </span>
              `).join('') : '<span class="muted">暂无关键词</span>'}
              ${isAddingKeyword
                ? `<input class="keyword-inline-input" placeholder="关键词" aria-label="新增关键词" data-keyword-add-input="${esc(key)}">`
                : `<button type="button" class="icon-button add" title="新增关键词" aria-label="新增关键词" data-keyword-add="${esc(key)}">+</button>`}
            </div>
            <button type="button" class="text-delete" data-category-delete="${esc(key)}">删除</button>
          </section>
        `;
      }).join('') : '<div class="muted">暂无分类</div>';
      document.querySelectorAll('[data-category-edit]').forEach(btn => btn.onclick = () => {
        editingCategoryKey = btn.dataset.categoryEdit;
        addingKeywordKey = null;
        renderCategoryRules();
        document.querySelector(`[data-category-edit-input="${CSS.escape(editingCategoryKey)}"]`)?.focus();
      });
      document.querySelectorAll('[data-category-edit-input]').forEach(input => {
        input.onkeydown = event => {
          if (event.key === 'Enter') saveCategoryRename(input, input.dataset.categoryEditInput).catch(err => alert(err.message));
          if (event.key === 'Escape') {
            editingCategoryKey = null;
            renderCategoryRules();
          }
        };
        input.onblur = () => saveCategoryRename(input, input.dataset.categoryEditInput).catch(err => {
          alert(err.message);
          renderCategoryRules();
        });
      });
      document.querySelectorAll('[data-category-delete]').forEach(btn => btn.onclick = async () => {
        const [type, category] = btn.dataset.categoryDelete.split('|||');
        scheduleCategoryDelete(type, category);
      });
      document.querySelectorAll('[data-keyword-add]').forEach(btn => btn.onclick = () => {
        addingKeywordKey = btn.dataset.keywordAdd;
        editingCategoryKey = null;
        renderCategoryRules();
        document.querySelector(`[data-keyword-add-input="${CSS.escape(addingKeywordKey)}"]`)?.focus();
      });
      document.querySelectorAll('[data-keyword-add-input]').forEach(input => {
        input.onkeydown = event => {
          if (event.key === 'Enter') saveKeywordAdd(input, input.dataset.keywordAddInput).catch(err => alert(err.message));
          if (event.key === 'Escape') {
            addingKeywordKey = null;
            renderCategoryRules();
          }
        };
        input.onblur = () => saveKeywordAdd(input, input.dataset.keywordAddInput).catch(err => {
          alert(err.message);
          renderCategoryRules();
        });
      });
      document.querySelectorAll('[data-rule-delete]').forEach(btn => btn.onclick = async () => {
        await api('/api/keyword-rule-delete', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({id: Number(btn.dataset.ruleDelete)})
        });
        keywordRules = await api('/api/keyword-rules');
        renderCategoryRules();
      });
    }

    function renderQuickFormOptions() {
      const typeValue = $('fType').value;
      $('fType').innerHTML = optionsHtml(entryTypes, typeValue || entryTypes[0]);
      if (entryTypes.includes(typeValue)) $('fType').value = typeValue;
      const accountValue = $('fAccount').value;
      $('fAccount').innerHTML = accountOptions.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('') +
        '<option value="__add_account__">+ 新增账户</option>';
      if (accountOptions.includes(accountValue)) $('fAccount').value = accountValue;
      const targetValue = $('fTargetAccount')?.value;
      if ($('fTargetAccount')) {
        $('fTargetAccount').innerHTML = accountOptions.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('') +
          '<option value="__add_account__">+ 新增账户</option>';
        if (accountOptions.includes(targetValue)) $('fTargetAccount').value = targetValue;
      }
      renderQuickCategoryOptions();
    }

    function renderQuickCategoryOptions() {
      const type = $('fType').value;
      const categoryValue = $('fCategory').value;
      const categories = categoriesForType(type);
      const isTransfer = type === '转账';
      const accountLabel = $('fAccountWrap')?.firstChild;
      if (accountLabel && accountLabel.nodeType === Node.TEXT_NODE) accountLabel.textContent = isTransfer ? '转出账户' : '账户';
      document.querySelector('.quick-main-row')?.classList.toggle('is-transfer', isTransfer);
      $('fTargetAccountWrap')?.classList.toggle('hidden', type !== '转账');
      // 转账也显示分类输入：贷款型月供需要「房贷/车贷」等还款类分类（普通转账留空即「转账」）
      // 分类已改为级联输入框：同步类型 + 清掉新类型下不合法的旧值
      $('fCategory').dataset.catType = type;
      if (categoryValue && !categories.includes(categoryValue)) $('fCategory').value = '';
    }

    function recentCategorySuggestions(type) {
      const allCategories = categoriesForType(type);
      const counts = new Map();
      entries
        .filter(entry => entry.type === type && entry.category && entry.category !== '转账' && allCategories.includes(entry.category))
        .slice(0, 30)
        .forEach(entry => counts.set(entry.category, (counts.get(entry.category) || 0) + 1));
      const ranked = [...counts.entries()]
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-Hans-CN'))
        .map(([category]) => category);
      return ranked.slice(0, 10);
    }

    function snapshotPendingSuggestions() {
      const types = [...new Set(pendingItems.map(item => item.type).filter(Boolean))];
      pendingSuggestionCache = types.reduce((acc, type) => {
        acc[type] = recentCategorySuggestions(type);
        return acc;
      }, {});
    }

    function categoryPicker(id, type) {
      const suggestions = pendingSuggestionCache[type] || [];
      const body = suggestions.map(category =>
        `<button class="chip" data-confirm-id="${esc(id)}" data-category="${esc(category)}">${esc(categoryLabel(category))}</button>`
      ).join('');
      return `<div class="category-picker" id="picker-${esc(id)}">
        <div class="category-group-title">推荐分类</div>
        <div class="category-group-options">${body || '<div class="muted">暂无可选分类</div>'}</div>
      </div>`;
    }

    function renderPendingSummary() {
      const counts = pendingItems.reduce((acc, item) => {
        const key = item.type || '未知';
        acc[key] = (acc[key] || 0) + 1;
        return acc;
      }, {});
      const rows = Object.entries(counts).sort((a, b) => b[1] - a[1]);
      $('pendingSummary').innerHTML = rows.length ? rows.map(([name, count]) =>
        `<div class="side-item"><span>${esc(name)}</span><strong>${count}</strong></div>`
      ).join('') : '<div class="muted">没有待确认条目</div>';
    }

    function renderPending() {
      renderPendingSummary();
      if (!pendingItems.length) {
        $('pendingList').innerHTML = '<div class="section muted">没有待确认条目</div>';
        return;
      }
      const visibleItems = pendingItems.slice(0, 4);
      $('pendingList').innerHTML = visibleItems.map((item, index) => {
        const tone = moneyTone(item);
        const id = item.ui_id || item.id;
        return `
          <article class="pending-row pending-card ${index === 0 ? 'is-front' : ''}" data-id="${esc(id)}" style="--i:${index}">
            <section>
              <div class="pending-title">
                <span class="badge">${esc(item.type || '未知')}</span>
                <span class="amount ${tone === 'income' ? 'income' : tone === 'expense' ? 'expense' : 'neutral-money'}">${signedMoney(item.amount_cents, tone)}</span>
              </div>
              <div class="detail-grid">
                <div><span>时间</span>${esc(item.transaction_time)}</div>
                ${item.account
                  ? `<div><span>账户</span>${esc(item.account)}</div>`
                  : `<label class="pending-object-field"><span>账户（必选）</span><select data-confirm-account="${esc(id)}">${optionsHtml(accountOptions, item.account, true)}</select></label>`}
                <div><span>转入账户</span>${esc(item.target_account || '')}</div>
                <div><span>参与人</span>${esc(item.participant || '')}</div>
                ${item.bill_source ? `<div><span>来源</span>${esc(item.bill_source)}</div>` : ''}
                <label class="pending-object-field">
                  <span>交易对象</span>
                  <input data-confirm-object="${esc(id)}" value="${esc(item.transaction_object || '')}" placeholder="对方名称、债务人或平台">
                </label>
                <label class="pending-object-field">
                  <span>备注</span>
                  <textarea data-confirm-note="${esc(id)}" rows="3" placeholder="补充说明">${esc(item.note || '')}</textarea>
                </label>
                <label class="pending-object-field refund-link-field ${item.type === '退款' ? '' : 'hidden'}" data-refund-link-wrap="${esc(id)}">
                  <span>关联原支出（关联后按净额计入支出）</span>
                  <select data-refund-link="${esc(id)}"><option value="">加载候选支出中…</option></select>
                </label>
                <button class="secondary save-meta-btn" data-save-meta="${esc(id)}" disabled title="交易对象或备注有修改后可保存">保存修改</button>
              </div>
            </section>
            <section>
              ${categoryPicker(id, item.type)}
              <div class="confirm-line">
                <select data-confirm-type="${esc(id)}" class="confirm-type-select" title="交易类型">${optionsHtml(entryTypes.length ? entryTypes : Object.keys(categoryTree), item.type || '支出')}</select>
                <input id="cat-${id}" class="cat-search" data-cat-type="${esc(item.type || '支出')}" value="${esc(item.category || '')}" placeholder="点击选择或搜索分类" autocomplete="off">
                <button class="secondary" data-pending-skip="${esc(id)}">跳过</button>
                <button class="primary" data-confirm-id="${esc(id)}">确认</button>
              </div>
              <label class="pending-object-field confirm-target-wrap ${item.type === '转账' ? '' : 'hidden'}" data-confirm-target-wrap="${esc(id)}">
                <span>转入账户（转账必选，须与付款账户不同）</span>
                <select data-confirm-target="${esc(id)}">${optionsHtml(accountOptions, item.target_account, true)}</select>
              </label>
            </section>
          </article>`;
      }).join('');
      hydratePendingRefundLinks();
    }

    // 待确认退款卡：加载"关联原支出"候选（同商户优先，唯一同商户自动预选）
    async function hydratePendingRefundLinks() {
      const selects = document.querySelectorAll('[data-refund-link]');
      for (const sel of selects) {
        const id = sel.dataset.refundLink;
        const item = pendingItems.find(it => String(it.ui_id || it.id) === String(id));
        if (!item) continue;
        try {
          const params = new URLSearchParams({merchant: item.transaction_object || '', time: item.transaction_time || '', amount: item.amount_cents || '', account: item.account || ''});
          const data = await api(`/api/refund-candidates?${params}`);
          const seen = new Set();
          const build = (list, tag) => (list || []).filter(c => !seen.has(c.id) && (seen.add(c.id), true)).map(c => {
            const linkedTag = c.linked_refund_count > 0 ? `（已被${c.linked_refund_count}笔退款关联）` : '';
            const tagText = (c.tags || []).length ? `【${c.tags.join('+')}】` : tag;
            return `<option value="${c.id}">${tagText}¥${plainMoney(c.amount_cents)} · ${esc(c.transaction_time)} · ${esc(c.category || '未分类')}${linkedTag}</option>`;
          }).join('');
          // 唯一的「金额相同+同商户」双命中才自动预选，避免误选
          const strong = (data.matches || []).filter(m => (m.tags || []).includes('金额相同') && (m.tags || []).includes('同商户'));
          sel.innerHTML = `<option value="">不关联（退款不冲减支出）</option>`
            + ((build(data.matches, '') + build(data.recent, '')) || '<option value="">未找到候选支出</option>');
          if (strong.length === 1) sel.value = String(strong[0].id);
        } catch (err) {
          sel.innerHTML = '<option value="">候选支出加载失败</option>';
        }
      }
    }

    async function confirmWithCategory(id, category) {
      if (pendingAnimating) return;
      pendingAnimating = true;
      try {
        const objectInput = document.querySelector(`[data-confirm-object="${CSS.escape(String(id))}"]`);
        const noteInput = document.querySelector(`[data-confirm-note="${CSS.escape(String(id))}"]`);
        const accountInput = document.querySelector(`[data-confirm-account="${CSS.escape(String(id))}"]`);
        const account = accountInput && accountInput.value.trim() ? accountInput.value.trim() : undefined;
        const typeSelect = document.querySelector(`[data-confirm-type="${CSS.escape(String(id))}"]`);
        const targetInput = document.querySelector(`[data-confirm-target="${CSS.escape(String(id))}"]`);
        const targetAccount = targetInput && targetInput.value ? targetInput.value : undefined;
        const item = pendingItems.find(item => String(item.ui_id || item.id) === String(id));
        const finalType = typeSelect ? typeSelect.value : item?.type;
        // 转账必须指定转入账户，缺失时给出明确提示而不是让后端报晦涩错误
        if (finalType === '转账' && !targetAccount) {
          $('status').textContent = '请先选择转入账户（转账必须指定目标账户）';
          return;
        }
        // 理财买入只能转入理财账户（kind=invest），防止误转入个人账户
        if (finalType === '转账' && category === '理财买入' && targetAccount && !investAccountNames().includes(targetAccount)) {
          $('status').textContent = '理财买入的转入账户必须是理财账户（如 理财·币安）';
          return;
        }
        const refundLinkInput = document.querySelector(`[data-refund-link="${CSS.escape(String(id))}"]`);
        const refundPairId = finalType === '退款' && refundLinkInput && refundLinkInput.value ? refundLinkInput.value : undefined;
        await api('/api/confirm', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({
            id: item ? item.id : id,
            source: item?.source,
            category,
            type: finalType,
            account,
            target_account: targetAccount,
            transaction_object: objectInput ? objectInput.value.trim() : '',
            note: noteInput ? noteInput.value.trim() : '',
            refund_pair_id: refundPairId,
            learn: false
          })
        });
        const card = document.querySelector(`.pending-card[data-id="${CSS.escape(String(id))}"]`);
        if (card) {
          card.classList.add('is-leaving');
          await new Promise(resolve => setTimeout(resolve, 260));
        }
        await refresh();
      } catch (err) {
        // 后端校验失败（如缺转入账户）必须可见，否则表现为"点了没反应"
        $('status').textContent = `确认失败：${err.message}`;
      } finally {
        pendingAnimating = false;
      }
    }

    async function confirmPending(id) {
      const category = $(`cat-${id}`).value.trim();
      if (!category) return;
      await confirmWithCategory(id, category);
    }

    function skipPending(id) {
      if (pendingAnimating) return;
      const index = pendingItems.findIndex(item => String(item.ui_id || item.id) === String(id));
      if (index < 0 || pendingItems.length < 2) return;
      pendingSkippedIds = pendingSkippedIds.filter(itemId => String(itemId) !== String(id));
      pendingSkippedIds.push(String(id));
      savePendingSkippedIds();
      const [item] = pendingItems.splice(index, 1);
      pendingItems.push(item);
      renderPending();
    }

    window.confirmPending = confirmPending;
    window.confirmWithCategory = confirmWithCategory;

    function pendingMetaState(id) {
      const item = pendingItems.find(item => String(item.ui_id || item.id) === String(id));
      const objectInput = document.querySelector(`[data-confirm-object="${CSS.escape(String(id))}"]`);
      const noteInput = document.querySelector(`[data-confirm-note="${CSS.escape(String(id))}"]`);
      if (!item) return {dirty: false, item, objectInput, noteInput};
      const objectChanged = objectInput && objectInput.value.trim() !== String(item.transaction_object || '').trim();
      const noteChanged = noteInput && noteInput.value.trim() !== String(item.note || '').trim();
      return {dirty: Boolean(objectChanged || noteChanged), item, objectInput, noteInput};
    }

    function updateSaveMetaButton(id) {
      const btn = document.querySelector(`[data-save-meta="${CSS.escape(String(id))}"]`);
      if (!btn) return;
      const {dirty} = pendingMetaState(id);
      btn.disabled = !dirty;
      btn.classList.toggle('is-dirty', dirty);
    }

    async function savePendingMeta(id) {
      const {dirty, item, objectInput, noteInput} = pendingMetaState(id);
      if (!dirty || !item) return;
      try {
        const objectValue = objectInput ? objectInput.value.trim() : String(item.transaction_object || '');
        const noteValue = noteInput ? noteInput.value.trim() : String(item.note || '');
        await api('/api/pending-update', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({
            id: item.id,
            source: item.source,
            transaction_object: objectValue,
            note: noteValue
          })
        });
        item.transaction_object = objectValue;
        item.note = noteValue;
        updateSaveMetaButton(id);
        $('status').textContent = '已保存交易对象/备注修改';
      } catch (err) {
        $('status').textContent = `保存失败：${err.message}`;
      }
    }
    window.savePendingMeta = savePendingMeta;

    $('pendingList').addEventListener('input', (event) => {
      const field = event.target.closest('[data-confirm-object], [data-confirm-note]');
      if (!field) return;
      const id = field.dataset.confirmObject ?? field.dataset.confirmNote;
      if (id !== undefined && id !== null && id !== '') updateSaveMetaButton(id);
    });

    // 待确认卡的转入账户选项：理财买入只允许转入理财账户（kind=invest）
    function investAccountNames() {
      return accountBalances.filter(a => a.kind === 'invest').map(a => a.name);
    }
    function pendingTargetOptions(id) {
      const catInput = $(`cat-${id}`);
      if (catInput && catInput.value.trim() === '理财买入') {
        const invest = investAccountNames();
        if (invest.length) return invest;
      }
      return accountOptions;
    }
    function refreshPendingTargetOptions(id) {
      const sel = document.querySelector(`[data-confirm-target="${CSS.escape(String(id))}"]`);
      if (!sel) return;
      sel.innerHTML = optionsHtml(pendingTargetOptions(id), sel.value, true);
    }

    $('pendingList').addEventListener('change', (event) => {
      const typeSelect = event.target.closest('[data-confirm-type]');
      if (!typeSelect) return;
      const id = typeSelect.dataset.confirmType;
      const newType = typeSelect.value;
      const catInput = $(`cat-${id}`);
      if (catInput) {
        catInput.value = '';
        catInput.dataset.catType = newType;
        if (catDropdownInput === catInput) closeCategoryDropdown();
      }
      const targetWrap = document.querySelector(`[data-confirm-target-wrap="${CSS.escape(String(id))}"]`);
      if (targetWrap) targetWrap.classList.toggle('hidden', newType !== '转账');
      if (newType === '转账') refreshPendingTargetOptions(id);
      const refundWrap = document.querySelector(`[data-refund-link-wrap="${CSS.escape(String(id))}"]`);
      if (refundWrap) {
        refundWrap.classList.toggle('hidden', newType !== '退款');
        if (newType === '退款') hydratePendingRefundLinks();
      }
      const picker = $(`picker-${id}`);
      if (picker) {
        let suggestions = recentCategorySuggestions(newType);
        // 转账类固定提供理财/还款快捷分类（C2C 买U等理财买入场景高频）
        if (newType === '转账') {
          suggestions = [...new Set(['理财买入', '理财卖出', '信用卡还款', ...suggestions])].slice(0, 10);
        }
        const body = suggestions.map(category =>
          `<button class="chip" data-confirm-id="${esc(id)}" data-category="${esc(category)}">${esc(categoryLabel(category))}</button>`
        ).join('');
        picker.innerHTML = `<div class="category-group-title">推荐分类</div><div class="category-group-options">${body || '<div class="muted">暂无可选分类</div>'}</div>`;
      }
    });

    // 分类输入变化（含浮层选中 pickCategory 派发的 input 事件）→ 联动刷新转入账户选项
    $('pendingList').addEventListener('input', (event) => {
      const catInput = event.target.closest('.cat-search');
      if (!catInput || !/^cat-/.test(catInput.id || '')) return;
      const typeSelect = document.querySelector(`[data-confirm-type="${CSS.escape(catInput.id.slice(4))}"]`);
      if (typeSelect && typeSelect.value === '转账') refreshPendingTargetOptions(catInput.id.slice(4));
    });

    $('pendingList').addEventListener('click', (event) => {
      const toggle = event.target.closest('[data-toggle-picker]');
      if (toggle) {
        const picker = $(`picker-${toggle.dataset.togglePicker}`);
        if (picker) picker.classList.toggle('hidden');
        return;
      }
      const skip = event.target.closest('[data-pending-skip]');
      if (skip) {
        skipPending(skip.dataset.pendingSkip);
        return;
      }
      const saveBtn = event.target.closest('[data-save-meta]');
      if (saveBtn) {
        if (!saveBtn.disabled) savePendingMeta(saveBtn.dataset.saveMeta);
        return;
      }
      const target = event.target.closest('[data-confirm-id]');
      if (!target) return;
      const id = target.dataset.confirmId;
      const category = target.dataset.category || $(`cat-${id}`)?.value.trim();
      if (!category) return;
      if (!target.dataset.category) {
        const typeSelect = document.querySelector(`[data-confirm-type="${CSS.escape(String(id))}"]`);
        const currentType = typeSelect ? typeSelect.value : pendingItems.find(item => String(item.ui_id || item.id) === String(id))?.type;
        if (!categoriesForType(currentType).includes(category)) {
          $('status').textContent = `分类「${categoryLabel(category)}」不属于当前类型「${currentType}」，请从列表中选择`;
          return;
        }
      }
      confirmWithCategory(id, category).catch(err => $('status').textContent = err.message);
    });

    const titles = {home: '财务总览', entries: '账单', confirm: '待确认分类', assets: '资产管家', invest: '投资理财', settings: '设置'};
    async function switchView(view) {
      await commitPendingDeletes({refreshAfter: true});
      document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
      const btn = document.querySelector(`nav button[data-view="${view}"]`);
      if (!btn) return;
      btn.classList.add('active');
      document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
      $(view).classList.remove('hidden');
      $('pageTitle').textContent = titles[view] || '账簿';
      $('periodPanel').classList.toggle('hidden', view !== 'home');
      // 退款自动关联工具条只在「退款KPI卡 → 账单」入口显示（jumpToEntriesByTone 随后按需打开）
      $('refundAutoLinkBar').classList.add('hidden');
      // 理财页不提供快速记账（收支记账与理财无关）
      const isInvest = view === 'invest';
      $('quickAdd').classList.toggle('hidden', isInvest);
      const headerClear = $('headerClearFilters');
      if (headerClear && view !== 'entries') headerClear.style.display = 'none';
      if (isInvest) $('quickForm').classList.add('hidden');
      if (view === 'entries') renderEntries();
      if (view === 'invest' && window.renderInvest) window.renderInvest();
      if (view === 'confirm') {
        snapshotPendingSuggestions();
        renderPending();
      }
    }
    document.querySelectorAll('nav button').forEach(btn => btn.onclick = () => switchView(btn.dataset.view).catch(err => alert(err.message)));

    window.addEventListener('pagehide', () => {
      commitPendingDeletes({keepalive: true, silent: true});
    });

    function jumpToEntriesByTone(tone) {
      const range = currentRange();
      entryFilters = {year: '', month: '', day: '', type: '', category: '', categoryPrefix: '', account: '', transaction_object: '', tone, start: range.start, end: range.end};
      entryPage = 1;
      // switchView 是 async 且末尾会隐藏工具条，必须等它完成后再按入口决定显隐
      switchView('entries').then(() => {
        $('refundAutoLinkBar').classList.toggle('hidden', tone !== 'refund');
      });
    }
    function jumpToEntriesByCategory(category, tone) {
      const range = currentRange();
      entryFilters = {year: '', month: '', day: '', type: '', category: '', categoryPrefix: '', account: '', transaction_object: '', tone: tone || '', start: range.start, end: range.end};
      if (String(category || '').includes('/')) entryFilters.category = category;
      else entryFilters.categoryPrefix = category;
      entryPage = 1;
      switchView('entries');
    }
    // 债权/负债归档卡「明细」：跳转账单页按账户筛选全部流水
    function jumpToEntriesByAccount(account) {
      // 明细语义=该账户全部流水：日期范围清空（建仓/期初记录可能在早前月份）
      entryFilters = {year: '', month: '', day: '', type: '', category: '', categoryPrefix: '', account, transaction_object: '', tone: '', start: '', end: ''};
      entryPage = 1;
      switchView('entries');
    }
    $('incomeKpi').onclick = () => jumpToEntriesByTone('income');
    $('expenseKpi').onclick = () => jumpToEntriesByTone('expense');
    $('refundKpi').onclick = () => jumpToEntriesByTone('refund');
    $('pendingKpi').onclick = () => switchView('confirm').catch(err => alert(err.message));
    // 活动表单（只做新建；改名走卡片行内编辑）
    $('activityAddBtn').onclick = () => {
      activityFormMode = 'create';
      renderActivities();
      $('activityName').focus();
    };
    $('activitySaveBtn').onclick = saveActivityForm;
    $('activityCancelBtn').onclick = () => { activityFormMode = null; renderActivities(); };
    $('activityName').addEventListener('keydown', e => { if (e.key === 'Enter') saveActivityForm(); });
    // 总览 KPI → 资产页对应分组：切换视图后平滑滚动到分组并闪烁高亮
    const jumpToAssetGroup = async (type) => {
      try {
        await switchView('assets');
        requestAnimationFrame(() => {
          const el = document.getElementById(`asset-group-${type}`);
          if (!el) return;
          el.scrollIntoView({behavior: 'smooth', block: 'start'});
          el.classList.remove('group-flash');
          void el.offsetWidth; // 重启动画
          el.classList.add('group-flash');
          setTimeout(() => el.classList.remove('group-flash'), 2000);
        });
      } catch (err) { alert(err.message); }
    };
    $('receivableKpi').onclick = () => jumpToAssetGroup('receivable');
    $('liabilityKpi').onclick = () => jumpToAssetGroup('liability');
    $('creditKpi').onclick = () => jumpToAssetGroup('credit');
    $('investKpi').onclick = () => jumpToAssetGroup('invest');
    function setExpenseCategoryLevel(level) {
      expenseCategoryLevel = level;
      localStorage.setItem('bookkeeping.expenseCategoryLevel', expenseCategoryLevel);
      updateOverview();
    }
    document.querySelector('.level-switch')?.addEventListener('click', event => {
      event.preventDefault();
      setExpenseCategoryLevel(expenseCategoryLevel === 'major' ? 'minor' : 'major');
    });

    document.querySelectorAll('[data-period-mode]').forEach(btn => btn.onclick = () => {
      periodMode = btn.dataset.periodMode;
      if (periodMode === 'year') yearOffset = 0;
      if (periodMode === 'month') monthOffset = 0;
      if (periodMode === 'more') selectedCycleIndex = 0;
      updateOverview();
    });
    $('rangeStartTrigger').onclick = (event) => openDatePicker('start', event.currentTarget);
    $('rangeEndTrigger').onclick = (event) => openDatePicker('end', event.currentTarget);
    $('datePickerYear').onchange = () => {
      pickerMonth = new Date(Number($('datePickerYear').value), pickerMonth.getMonth(), 1);
      renderDatePicker();
    };
    $('datePickerMonth').onchange = () => {
      pickerMonth = new Date(pickerMonth.getFullYear(), Number($('datePickerMonth').value), 1);
      renderDatePicker();
    };
    $('datePickerGrid').onclick = (event) => {
      const target = event.target.closest('[data-date]');
      if (!target || !activeDateTarget) return;
      const range = currentRange();
      customStart = activeDateTarget === 'start' ? target.dataset.date : range.start;
      customEnd = activeDateTarget === 'end' ? target.dataset.date : range.end;
      if (customStart && customEnd && customStart > customEnd) {
        [customStart, customEnd] = [customEnd, customStart];
      }
      periodMode = 'all';
      closeDatePicker();
      updateOverview();
    };
    document.addEventListener('click', (event) => {
      if ($('datePicker').classList.contains('hidden')) return;
      if (event.target.closest('#datePicker') || event.target.closest('.date-trigger')) return;
      closeDatePicker();
    });
    document.querySelectorAll('[data-cycle]').forEach(btn => btn.onclick = () => {
      cycleMode = btn.dataset.cycle;
      selectedCycleIndex = 0;
      updateOverview();
    });
    $('prevCycle').onclick = () => {
      if (periodMode === 'year') yearOffset -= 1;
      else if (periodMode === 'month') monthOffset -= 1;
      else selectedCycleIndex += 1;
      updateOverview();
    };
    $('nextCycle').onclick = () => {
      if (periodMode === 'year') yearOffset += 1;
      else if (periodMode === 'month') monthOffset += 1;
      else selectedCycleIndex = Math.max(0, selectedCycleIndex - 1);
      updateOverview();
    };

    $('quickAdd').onclick = () => $('quickForm').classList.toggle('hidden');
    $('refundAutoLinkBtn').onclick = async () => {
      const btn = $('refundAutoLinkBtn');
      const box = $('refundAutoLinkReport');
      btn.disabled = true;
      box.textContent = '正在分析未关联退款…';
      try {
        const rep = await api('/api/refund-auto-link', { method: 'POST' });
        const byId = new Map(entries.map(e => [Number(e.id), e]));
        const parts = [];
        if (rep.auto.length) {
          parts.push(`✅ 自动关联 ${rep.auto.length} 笔：` + rep.auto.map(x => {
            const e = byId.get(Number(x.expense_id));
            return `¥${plainMoney(byId.get(Number(x.refund_id))?.amount_cents)}→${esc(e ? e.transaction_object : `#${x.expense_id}`)}(${x.confidence >= 1 ? '99%' : `${Math.round(x.confidence * 100)}%`})`;
          }).join('、'));
        }
        if (rep.manual.length) parts.push(`⚠️ ${rep.manual.length} 笔置信不足留人工，请逐张卡片选择`);
        if (rep.no_candidate.length) {
          parts.push(`❌ ${rep.no_candidate.length} 笔无匹配扣款（原支出可能在导入范围外）：`
            + rep.no_candidate.map(x => {
              const rf = byId.get(Number(x.refund_id));
              return rf ? `¥${plainMoney(rf.amount_cents)} ${esc((rf.transaction_time || '').slice(0, 16))}` : `#${x.refund_id}`;
            }).join('、'));
        }
        box.innerHTML = parts.length ? parts.join('<br>') : '没有需要关联的退款';
        await refresh();
      } catch (err) {
        box.textContent = `自动关联失败：${err.message}`;
      } finally {
        btn.disabled = false;
      }
    };
    $('toggleQuickMore').onclick = () => {
      const panel = $('quickMoreFields');
      const expanded = panel.classList.toggle('hidden') === false;
      $('toggleQuickMore').textContent = expanded ? '收起' : '展开';
    };
    function applyTheme(dark) {
      document.body.dataset.theme = dark ? 'dark' : '';
      $('themeToggle').checked = dark;
      localStorage.setItem('bookkeeping.theme', dark ? 'dark' : 'light');
      if (summaryData) updateOverview();
    }
    function applyColorMode(inverted) {
      document.body.dataset.colorInverted = inverted ? 'true' : '';
      $('colorInvert').checked = inverted;
      localStorage.setItem('bookkeeping.colorInverted', inverted ? 'true' : 'false');
      if (summaryData) updateOverview();
    }
    $('themeToggle').onchange = () => applyTheme($('themeToggle').checked);
    $('colorInvert').onchange = () => applyColorMode($('colorInvert').checked);
    $('addCategoryRule').onclick = async () => {
      const type = $('categoryRuleType').value;
      const major = $('newCategoryMajor').value.trim();
      const minor = $('newCategoryMinor').value.trim();
      const status = $('status');
      const fail = msg => { status.textContent = msg; return false; };
      if (!major) return fail('请填写一级分类');
      if (minor.includes('/')) return fail('二级分类不能包含「/」');
      const tree = categoryTree[type] || {};
      const majorExists = Object.prototype.hasOwnProperty.call(tree, major);
      // 去重：一级已存在且没填二级 → 不能保存；一级/二级组合已存在 → 不能保存
      if (majorExists && !minor) return fail(`一级分类「${major}」已存在，不能重复添加`);
      if (minor && Array.isArray(tree[major]) && tree[major].includes(minor)) {
        return fail(`分类「${major}/${minor}」已存在，不能重复添加`);
      }
      const value = minor ? `${major}/${minor}` : major;
      try {
        await api('/api/category-add', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({type, category: value})
        });
      } catch (err) { status.textContent = err.message; return; }
      $('newCategoryMajor').value = '';
      $('newCategoryMinor').value = '';
      await refresh();
      status.textContent = `已新增分类：${categoryLabel(value)}`;
    };
    // 类型切换：一级选择器的候选树跟随变化
    $('categoryRuleType').onchange = () => {
      $('newCategoryMajor').dataset.catType = $('categoryRuleType').value;
    };
    // 默认暗色终端主题；用户切过「浅色」则尊重其选择
    applyTheme(localStorage.getItem('bookkeeping.theme') !== 'light');
    applyColorMode(localStorage.getItem('bookkeeping.colorInverted') === 'true');
    $('search').oninput = () => {
      entryPage = 1;
      renderEntries();
    };
    $('fType').onchange = renderQuickCategoryOptions;
    async function addAccountFromQuick(selectId) {
      const select = $(selectId);
      if (select.value !== '__add_account__') return;
      const name = prompt('新增账户名称');
      if (!name || !name.trim()) {
        renderQuickFormOptions();
        return;
      }
      await api('/api/account-add', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({name: name.trim()})
      });
      accountOptions = await api('/api/account-names');
      renderQuickFormOptions();
      select.value = name.trim();
    }

    $('fAccount').onchange = async () => {
      await addAccountFromQuick('fAccount');
    };
    $('fTargetAccount').onchange = async () => {
      await addAccountFromQuick('fTargetAccount');
    };
    // 快速记账"+ 新增分类"（级联浮层底部固定项触发）
    async function promptAddQuickCategory() {
      const category = prompt($('fType').value === '支出' ? '新增分类，格式：一级/二级' : '新增分类名称');
      if (!category || !category.trim()) return;
      await api('/api/category-add', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({type: $('fType').value, category: category.trim()})
      });
      [categoryOptions, categoryTree] = await Promise.all([api('/api/categories'), api('/api/category-tree')]);
      entryTypes = Object.keys(categoryTree);
      renderQuickCategoryOptions();
      $('fCategory').value = category.trim();
    };
    $('saveEntry').onclick = async () => {
      const isTransfer = $('fType').value === '转账';
      if ($('fAccount').value === '__add_account__' || (isTransfer && $('fTargetAccount').value === '__add_account__') || (!isTransfer && !$('fCategory').value.trim())) return;
      // 防双计前端硬拦（与后端双保险）：贷款型（还款口径）负债账户不记支出
      if (!isTransfer) {
        const acc = accountBalances.find(r => r.name === $('fAccount').value);
        if (acc && acc.kind === 'liability' && acc.expense_mode === 'repay') {
          $('status').textContent = '还款口径账户不记支出，月供请用「转账」+分类「房贷/车贷」';
          return;
        }
      }
      const payload = {
          type: $('fType').value,
          amount: $('fAmount').value,
          transaction_time: $('fTime').value ? $('fTime').value.replace('T', ' ') : '',
          account: $('fAccount').value,
          target_account: isTransfer ? $('fTargetAccount').value : '',
          // 转账分类可留空（即通用「转账」），月供还款时预填 房贷/车贷/信用卡还款
          category: isTransfer ? ($('fCategory').value.trim() || '转账') : $('fCategory').value,
          transaction_object: $('fTransactionObject').value.trim(),
          participant: '自己',
          note: $('fNote').value.trim(),
          learn: $('fLearn').checked
      };
      try {
        const result = await api('/api/add', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify(payload)
        });
        if (result && result.warning) $('status').textContent = result.warning;
      } catch (err) {
        $('status').textContent = err.message;
        return;
      }
      $('fAmount').value = '';
      $('fTransactionObject').value = '';
      $('fNote').value = '';
      $('fTime').value = nowDatetimeLocal();
      refresh();
    };

    refresh().catch(err => $('status').textContent = err.message);
