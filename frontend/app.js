document.addEventListener('DOMContentLoaded', () => {
    // UI Elements
    const statusRing = document.getElementById('status-ring');
    const statusDot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');
    const equityValue = document.getElementById('equity-value');
    const balanceValue = document.getElementById('balance-value');
    const unrealizedValue = document.getElementById('unrealized-value');
    const positionsBody = document.getElementById('positions-body');
    const logsContainer = document.getElementById('logs-container');

    // Chart Setup
    const ctx = document.getElementById('equityChart').getContext('2d');
    
    // Gradient for the chart fill
    const gradientFill = ctx.createLinearGradient(0, 0, 0, 400);
    gradientFill.addColorStop(0, 'rgba(0, 242, 254, 0.5)');
    gradientFill.addColorStop(1, 'rgba(0, 242, 254, 0.0)');

    const equityChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Account Equity',
                data: [],
                borderColor: '#00f2fe',
                backgroundColor: gradientFill,
                borderWidth: 2,
                pointRadius: 0,
                pointHoverRadius: 6,
                fill: true,
                tension: 0.4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    backgroundColor: 'rgba(25, 27, 36, 0.9)',
                    titleColor: '#94a3b8',
                    bodyColor: '#f0f0f5',
                    borderColor: 'rgba(255, 255, 255, 0.1)',
                    borderWidth: 1
                }
            },
            scales: {
                x: {
                    display: true, // Show x-axis labels
                    ticks: {
                        color: '#94a3b8',
                        maxTicksLimit: 10,
                        callback: function(val, index) {
                            // Extract just the HH:mm from the timestamp string
                            const label = this.getLabelForValue(val);
                            if (!label) return '';
                            const d = new Date(label);
                            return d.getHours().toString().padStart(2, '0') + ':' + 
                                   d.getMinutes().toString().padStart(2, '0');
                        }
                    },
                    grid: {
                        color: 'rgba(255, 255, 255, 0.05)',
                        drawBorder: false
                    }
                },
                y: {
                    grid: {
                        color: 'rgba(255, 255, 255, 0.05)',
                        drawBorder: false
                    },
                    ticks: {
                        color: '#94a3b8',
                        callback: function(value) {
                            return '$' + value.toLocaleString();
                        }
                    }
                }
            },
            interaction: {
                mode: 'nearest',
                axis: 'x',
                intersect: false
            }
        }
    });

    // Formatting utilities
    const formatCurrency = (val) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(val);
    const formatDate = (isoString) => {
        const d = new Date(isoString);
        return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`;
    };

    // State updaters
    function setConnectionStatus(isLive) {
        const indicator = document.querySelector('.status-indicator');
        if (isLive) {
            indicator.classList.remove('status-offline');
            indicator.classList.add('status-live');
            statusText.textContent = 'System Live';
        } else {
            indicator.classList.remove('status-live');
            indicator.classList.add('status-offline');
            statusText.textContent = 'System Offline';
        }
    }

    function updateHeaderStats(equity, balance, unrealized) {
        equityValue.textContent = formatCurrency(equity);
        balanceValue.textContent = formatCurrency(balance);
        
        unrealizedValue.textContent = formatCurrency(unrealized);
        unrealizedValue.className = 'value ' + (unrealized >= 0 ? 'positive' : 'negative');
    }

    function renderPositions(positions) {
        positionsBody.innerHTML = '';
        if (positions.length === 0) {
            positionsBody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #94a3b8;">No open positions</td></tr>';
            return;
        }

        positions.forEach(pos => {
            const tr = document.createElement('tr');
            
            // Handle different shapes (broker payload vs DB payload)
            const symbol = pos.symbol;
            let side = (pos.side || 'BUY').toUpperCase();
            if (side === 'LONG') side = 'BUY';
            if (side === 'SHORT') side = 'SELL';
            const sideClass = side === 'BUY' ? 'side-buy' : 'side-sell';
            const qty = parseFloat(pos.qty).toFixed(4);
            const entry = parseFloat(pos.avg_entry_price || pos.entry_price || 0);
            
            // Try to extract SL / TP if available
            const sl = pos.stop_loss ? formatCurrency(pos.stop_loss) : '-';
            const tp = pos.take_profit ? formatCurrency(pos.take_profit) : '-';

            const availableQty = parseFloat(pos.available_qty);
            const isClosing = availableQty === 0;
            const actionButton = isClosing 
                ? `<button class="close-btn" disabled>Closing...</button>`
                : `<button class="close-btn" data-symbol="${symbol}">Close</button>`;

            tr.innerHTML = `
                <td style="font-weight: 600;">${symbol}</td>
                <td class="${sideClass}">${side}</td>
                <td>${qty}</td>
                <td>${formatCurrency(entry)}</td>
                <td>${sl}</td>
                <td>${tp}</td>
                <td style="text-align: right;">
                    ${actionButton}
                </td>
            `;
            positionsBody.appendChild(tr);
        });
    }

    function addLog(logData) {
        const div = document.createElement('div');
        div.className = `log-entry log-${logData.level}`;
        
        const timeStr = logData.timestamp ? formatDate(logData.timestamp) : formatDate(new Date().toISOString());
        
        div.innerHTML = `<span class="log-time">[${timeStr}]</span> ${logData.message}`;
        logsContainer.prepend(div);
        
        // Keep only last 100 logs in DOM
        if (logsContainer.children.length > 100) {
            logsContainer.removeChild(logsContainer.lastChild);
        }
    }

    // Close position handler
    positionsBody.addEventListener('click', async (e) => {
        if (e.target.classList.contains('close-btn')) {
            const symbol = e.target.dataset.symbol;
            
            const password = prompt(`Enter password to close position for ${symbol}:`);
            if (!password) return;
            
            if (confirm(`Are you sure you want to close the position for ${symbol}?`)) {
                e.target.disabled = true;
                e.target.textContent = 'Closing...';
                try {
                    const res = await fetch(`/api/positions/${symbol}/close?password=${encodeURIComponent(password)}`, { method: 'POST' });
                    if (!res.ok) {
                        if (res.status === 403) {
                            alert("Incorrect password.");
                            throw new Error('Incorrect password');
                        }
                        throw new Error('Network response was not ok');
                    }
                } catch (err) {
                    console.error("Failed to close position", err);
                    e.target.disabled = false;
                    e.target.textContent = 'Close';
                }
            }
        }
    });

    let currentMaxPoints = 5000;

    function appendChartData(timestamp, equity) {
        equityChart.data.labels.push(timestamp);
        equityChart.data.datasets[0].data.push(equity);
        
        if (equityChart.data.labels.length > currentMaxPoints) {
            equityChart.data.labels.shift();
            equityChart.data.datasets[0].data.shift();
        }
        
        equityChart.update();
    }

    async function fetchEquityData(timeframe = 'ALL') {
        try {
            const equityRes = await fetch(`/api/equity?timeframe=${timeframe}&limit=5000`);
            const equityHistory = await equityRes.json();
            
            equityChart.data.labels = [];
            equityChart.data.datasets[0].data = [];
            
            if (equityHistory && equityHistory.length > 0) {
                equityHistory.forEach(record => {
                    equityChart.data.labels.push(record.timestamp);
                    equityChart.data.datasets[0].data.push(record.equity);
                });
                equityChart.update();
                
                const latest = equityHistory[equityHistory.length - 1];
                updateHeaderStats(latest.equity, latest.balance, latest.unrealized_pnl);
            }
        } catch (e) {
            console.error("Error fetching equity data:", e);
        }
    }

    // Timeframe selector listeners
    const tfButtons = document.querySelectorAll('.tf-btn');
    tfButtons.forEach(btn => {
        btn.addEventListener('click', async (e) => {
            tfButtons.forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            const tf = e.target.dataset.tf;
            await fetchEquityData(tf);
        });
    });

    // Initial Data Fetch
    async function fetchInitialData() {
        try {
            // Check status
            const statusRes = await fetch('/api/status');
            const status = await statusRes.json();
            setConnectionStatus(status.running);

            // Fetch recent equity using the new function
            await fetchEquityData('ALL');

            // Fetch positions
            const posRes = await fetch('/api/positions');
            const positions = await posRes.json();
            renderPositions(positions);

            // Fetch logs
            const logsRes = await fetch('/api/logs');
            const logs = await logsRes.json();
            if (logs && logs.length > 0) {
                logs.reverse().forEach(addLog);
            }

        } catch (e) {
            console.error("Error fetching initial data:", e);
        }
    }

    // WebSocket Connection
    function connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;
        
        const ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            console.log("WebSocket connected");
            setConnectionStatus(true);
        };

        ws.onmessage = (event) => {
            if (event.data === "pong") return;
            
            try {
                const payload = JSON.parse(event.data);
                const { type, data } = payload;

                switch (type) {
                    case 'equity_update':
                        updateHeaderStats(data.equity, data.balance, data.unrealized_pnl);
                        appendChartData(data.timestamp, data.equity);
                        break;
                    
                    case 'position_update':
                        renderPositions(data);
                        break;
                    
                    case 'trade_opened':
                    case 'trade_closed':
                        // Refetch positions to be safe, or just wait for next position_update
                        fetch('/api/positions').then(r => r.json()).then(renderPositions);
                        break;

                    case 'log':
                        addLog(data);
                        break;
                }
            } catch (e) {
                console.error("Error parsing WS message:", e);
            }
        };

        ws.onclose = () => {
            console.log("WebSocket disconnected. Reconnecting in 5s...");
            setConnectionStatus(false);
            setTimeout(connectWebSocket, 5000);
        };
        
        ws.onerror = (err) => {
            console.error("WebSocket error:", err);
            ws.close();
        };

        // Keep-alive
        setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) {
                ws.send("ping");
            }
        }, 30000);
    }

    // Boot
    fetchInitialData().then(() => {
        connectWebSocket();
    });
});
