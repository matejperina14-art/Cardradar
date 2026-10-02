<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CardRadar 🔍 - Porovnávač Pokémon Kariet</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { background-color: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; display: flex; justify-content: center; }
        .container { max-width: 760px; width: 100%; }
        header { text-align: center; margin-bottom: 20px; }
        h1 { color: #facc15; font-size: 2.2rem; margin: 0 0 6px 0; font-weight: 800; }
        p.subtitle { color: #94a3b8; font-size: 0.9rem; margin: 0; }
        
        .search-form { display: flex; gap: 8px; margin-bottom: 10px; }
        input[type="text"], input[type="number"] { padding: 12px; border-radius: 10px; border: 1px solid #334155; background-color: #1e293b; color: #ffffff; font-size: 0.95rem; outline: none; }
        input[type="text"] { flex: 1; }
        input[type="text"]:focus, input[type="number"]:focus { border-color: #facc15; }
        button { padding: 12px 20px; border-radius: 10px; border: none; background-color: #facc15; color: #0f172a; font-weight: bold; font-size: 0.95rem; cursor: pointer; }

        .advanced-filters { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 12px; margin-bottom: 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; font-size: 0.85rem; color: #94a3b8; }
        .price-inputs { display: flex; align-items: center; gap: 6px; }
        .price-inputs input { width: 75px; text-align: center; padding: 6px; }

        .quick-tags { display: flex; gap: 6px; margin-bottom: 20px; flex-wrap: wrap; align-items: center; }
        .quick-tags span { font-size: 0.8rem; color: #64748b; }
        .tag-btn { background: #1e293b; border: 1px solid #334155; color: #94a3b8; padding: 4px 10px; border-radius: 12px; font-size: 0.8rem; cursor: pointer; }
        .tag-btn:hover { border-color: #facc15; color: #facc15; }

        .controls-bar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; }
        .filter-bar { display: flex; gap: 6px; flex-wrap: wrap; }
        .filter-btn { background: #1e293b; border: 1px solid #334155; color: #94a3b8; padding: 6px 12px; border-radius: 20px; font-size: 0.85rem; cursor: pointer; }
        .filter-btn.active { background: #facc15; color: #0f172a; font-weight: bold; border-color: #facc15; }
        .sort-select { background: #1e293b; color: #f8fafc; border: 1px solid #334155; padding: 6px 10px; border-radius: 8px; font-size: 0.85rem; outline: none; }

        #loading-spinner { display: none; text-align: center; padding: 40px 0; }
        .spinner { width: 40px; height: 40px; border: 4px solid #334155; border-top: 4px solid #facc15; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 12px auto; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }

        .card-preview-container { background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 20px; border: 1px solid #334155; display: none; }
        .card-preview-header { display: flex; align-items: center; gap: 16px; margin-bottom: 16px; }
        .card-preview-header img { width: 80px; height: auto; border-radius: 6px; }
        .card-preview-info h2 { margin: 0 0 4px 0; font-size: 1.2rem; color: #facc15; }
        .card-preview-info p { margin: 0; color: #94a3b8; font-size: 0.85rem; }
        .chart-box { background: #0f172a; border-radius: 8px; padding: 12px; border: 1px solid #334155; }

        .card-list { display: flex; flex-direction: column; gap: 10px; }
        .card-item { background-color: #1e293b; border-radius: 12px; padding: 14px 16px; border: 1px solid #334155; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
        .card-title { font-weight: 600; font-size: 0.95rem; color: #f8fafc; margin-bottom: 6px; }
        .badge { display: inline-block; font-size: 0.75rem; padding: 2px 8px; border-radius: 6px; font-weight: 600; }
        .badge-sk { background-color: #0284c7; color: #ffffff; }
        .badge-cz { background-color: #e11d48; color: #ffffff; }
        .badge-best { background-color: #16a34a; color: #ffffff; margin-left: 4px; }
        .badge-stock { background-color: #334155; color: #4ade80; margin-left: 4px; }

        .card-action { text-align: right; }
        .price-eur { font-size: 1.2rem; font-weight: 700; color: #4ade80; }
        .price-original { font-size: 0.75rem; color: #64748b; margin-bottom: 4px; }
        .btn-buy { display: inline-block; text-decoration: none; background-color: #facc15; color: #0f172a; font-size: 0.8rem; font-weight: 700; padding: 6px 12px; border-radius: 6px; }
        .empty-state { text-align: center; color: #64748b; padding: 30px 0; display: none; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>CardRadar 🔍</h1>
            <p class="subtitle">Porovnávač cien Pokémon kariet zo SK & CZ e-shopov</p>
        </header>

        <div class="search-form">
            <input type="text" id="searchInput" placeholder="Zadaj názov karty (napr. Charizard, Pikachu)...">
            <button onclick="performSearch()">Hľadať</button>
        </div>

        <div class="advanced-filters">
            <label style="display: flex; align-items: center; gap: 6px; cursor: pointer;">
                <input type="checkbox" id="inStockFilter" checked>
                <span>Len skladom</span>
            </label>
            <div class="price-inputs">
                <span>Cena (€):</span>
                <input type="number" id="minPrice" placeholder="Min" step="0.1">
                <span>-</span>
                <input type="number" id="maxPrice" placeholder="Max" step="0.1">
            </div>
        </div>

        <div class="quick-tags">
            <span>Rýchle vyhľadávanie:</span>
            <button class="tag-btn" onclick="searchTag('Charizard')">Charizard</button>
            <button class="tag-btn" onclick="searchTag('Pikachu')">Pikachu</button>
            <button class="tag-btn" onclick="searchTag('Mewtwo')">Mewtwo</button>
            <button class="tag-btn" onclick="searchTag('Gengar')">Gengar</button>
            <button class="tag-btn" onclick="searchTag('Rayquaza')">Rayquaza</button>
        </div>

        <div id="loading-spinner">
            <div class="spinner"></div>
            <p style="color: #94a3b8; font-size: 0.9rem;">Načítavam dáta kariet...</p>
        </div>

        <div id="content-area">
            <div class="controls-bar" id="controlsBar" style="display: none;">
                <div class="filter-bar">
                    <button class="filter-btn active" onclick="setRegion('all', this)">Všetky</button>
                    <button class="filter-btn" onclick="setRegion('SK', this)">🇸🇰 Len SK</button>
                    <button class="filter-btn" onclick="setRegion('CZ', this)">🇨🇿 Len CZ</button>
                </div>
                <div>
                    <select class="sort-select" id="sortSelect" onchange="renderResults()">
                        <option value="asc">Od najlacnejších</option>
                        <option value="desc">Od najdrahších</option>
                    </select>
                </div>
            </div>

            <div class="card-preview-container" id="previewContainer">
                <div class="card-preview-header">
                    <img id="cardImage" src="" alt="Karta">
                    <div class="card-preview-info">
                        <h2 id="cardTitleHeading"></h2>
                        <p>Oficiálna karta z Pokémon TCG databázy</p>
                    </div>
                </div>
                <div class="chart-box">
                    <canvas id="priceChart" height="120"></canvas>
                </div>
            </div>

            <div class="card-list" id="cardList"></div>
            <div class="empty-state" id="emptyState">Pre zadané filtre sa nenašli žiadne dostupné karty.</div>
        </div>
    </div>

    <script>
        let currentRegion = 'all';
        let allResults = [];
        let priceChartInstance = null;

        function searchTag(query) {
            document.getElementById('searchInput').value = query;
            performSearch();
        }

        function setRegion(region, element) {
            currentRegion = region;
            document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
            element.classList.add('active');
            renderResults();
        }

        async function performSearch() {
            const query = document.getElementById('searchInput').value.trim();
            if (!query) return;

            document.getElementById('loading-spinner').style.display = 'block';
            document.getElementById('content-area').style.display = 'none';

            try {
                // Načítanie obrázka z oficiálnej Pokémon TCG API
                const tcgRes = await fetch(`https://api.pokemontcg.io/v2/cards?q=name:${encodeURIComponent(query)}&pageSize=1`);
                const tcgData = await tcgRes.json();
                
                const previewContainer = document.getElementById('previewContainer');
                if (tcgData.data && tcgData.data.length > 0) {
                    document.getElementById('cardImage').src = tcgData.data[0].images.small;
                    document.getElementById('cardTitleHeading').innerText = query.charAt(0).toUpperCase() + query.slice(1);
                    previewContainer.style.display = 'block';
                } else {
                    previewContainer.style.display = 'none';
                }

                // Ukážkové dáta pre statické HTML (keďže priamy scraping cez CORS v čistom prehliadači blokujú e-shopy)
                // Pre plnú funkčnosť scraperov odporúčam serverové riešenie, toto je čistý HTML/JS dizajn na ukážku:
                allResults = [
                    { title: query + " - Holo Edition", price_eur: 14.50, shop: "CardyX (SK)", country: "SK", link: "https://www.cardyx.sk", in_stock: true },
                    { title: query + " - Common", price_eur: 3.20, shop: "iHRYsko (SK)", country: "SK", link: "https://www.ihrysko.sk", in_stock: true },
                    { title: query + " - Special Art", price_eur: 35.00, shop: "Černý Rytíř (CZ)", country: "CZ", price_raw: "850 Kč", link: "https://www.cernyrytir.cz", in_stock: true },
                    { title: query + " - Booster Pack", price_eur: 6.50, shop: "Veselý Drak (CZ)", country: "CZ", price_raw: "160 Kč", link: "https://www.vesely-drak.cz", in_stock: false }
                ];

                document.getElementById('controlsBar').style.display = 'flex';
                renderResults();
                renderChart();

            } catch (err) {
                console.error(err);
            } finally {
                document.getElementById('loading-spinner').style.display = 'none';
                document.getElementById('content-area').style.display = 'block';
            }
        }

        function renderResults() {
            const inStockOnly = document.getElementById('inStockFilter').checked;
            const minPrice = parseFloat(document.getElementById('minPrice').value) || 0;
            const maxPrice = parseFloat(document.getElementById('maxPrice').value) || 999999;
            const sortBy = document.getElementById('sortSelect').value;

            let filtered = allResults.filter(item => {
                if (currentRegion !== 'all' && item.country !== currentRegion) return false;
                if (inStockOnly && !item.in_stock) return false;
                if (item.price_eur < minPrice || item.price_eur > maxPrice) return false;
                return true;
            });

            filtered.sort((a, b) => sortBy === 'asc' ? a.price_eur - b.price_eur : b.price_eur - a.price_eur);

            const listEl = document.getElementById('cardList');
            const emptyEl = document.getElementById('emptyState');
            listEl.innerHTML = '';

            if (filtered.length === 0) {
                emptyEl.style.display = 'block';
                return;
            }
            emptyEl.style.display = 'none';

            filtered.forEach((item, index) => {
                const isBest = (index === 0 && sortBy === 'asc');
                listEl.innerHTML += `
                    <div class="card-item">
                        <div>
                            <div class="card-title">${item.title}</div>
                            <span class="badge ${item.country === 'SK' ? 'badge-sk' : 'badge-cz'}">${item.shop}</span>
                            ${item.in_stock ? '<span class="badge badge-stock">Skladom ✓</span>' : ''}
                            ${isBest ? '<span class="badge badge-best">TOP CENA ★</span>' : ''}
                        </div>
                        <div class="card-action">
                            <div class="price-eur">${item.price_eur.toFixed(2)} €</div>
                            ${item.price_raw ? `<div class="price-original">(${item.price_raw})</div>` : ''}
                            <a href="${item.link}" target="_blank" rel="noopener noreferrer" class="btn-buy">Kúpiť ↗</a>
                        </div>
                    </div>
                `;
            });
        }

        function renderChart() {
            const ctx = document.getElementById('priceChart').getContext('2d');
            if (priceChartInstance) priceChartInstance.destroy();

            priceChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: ['Pondelok', 'Utorok', 'Streda', 'Štvrtok', 'Piatok'],
                    datasets: [{
                        label: 'Vývoj priemernej ceny (€)',
                        data: [12.0, 13.5, 12.8, 14.1, 13.8],
                        borderColor: '#4ade80',
                        backgroundColor: 'rgba(74, 222, 128, 0.1)',
                        fill: true,
                        tension: 0.3
                    }]
                },
                options: {
                    responsive: true,
                    plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
                    scales: {
                        x: { ticks: { color: '#64748b' }, grid: { color: '#334155' } },
                        y: { ticks: { color: '#64748b' }, grid: { color: '#334155' } }
                    }
                }
            });
        }
    </script>
</body>
</html>
