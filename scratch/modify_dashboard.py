import io

with io.open('templates/dashboard.html', 'r', encoding='utf-8') as f:
    content = f.read()

new_btn = '<button class="btn-report no-print" style="background-color: #f39c12; margin-right: 10px;" onclick="openFinancialModal()">💰 View Expenditure & ROI Report</button>\n                '
content = content.replace('<button class="btn-comp no-print"', new_btn + '<button class="btn-comp no-print"')

modal_html = """
    <!-- FINANCIAL EXPENDITURE MODAL -->
    <div class="modal-overlay" id="financialModal">
        <div class="modal-card" style="border-top: 6px solid #f39c12; max-width: 600px;">
            <h3 style="color: #2c3e50; margin-bottom: 5px;">💰 Expenditure & ROI Report</h3>
            <p style="color: #7f8c8d; font-size: 0.9rem; margin-bottom: 20px;" id="finModalPeriodLabel">Loading...</p>
            
            <h4 style="margin-bottom: 10px; border-bottom: 1px solid #ddd; padding-bottom: 5px;">Balance Sheet Overview</h4>
            <div class="breakdown-row"><span>Beginning Balance</span><span>₱0.00</span></div>
            <div class="breakdown-row"><span>Accounts Receivable (Unpaid)</span><span id="finAR">₱0.00</span></div>
            <div class="breakdown-row"><span>Accounts Payable (School IGP)</span><span id="finAP">₱0.00</span></div>
            <div class="breakdown-row" style="margin-bottom: 20px;"><span>Production Cost (Paper Reams)</span><span id="finProdCost">₱0.00</span></div>
            
            <h4 style="margin-bottom: 10px; border-bottom: 1px solid #ddd; padding-bottom: 5px;">Income Statement</h4>
            <div class="breakdown-row"><span>Gross Sales Revenue</span><span id="finGross">₱0.00</span></div>
            <div class="breakdown-row"><span style="color: #e74c3c;">Less: 1% IT Dept Share</span><span id="finITShare" style="color: #e74c3c;">-₱0.00</span></div>
            <div class="breakdown-row"><span style="color: #e74c3c;">Less: 1% CS Dept Share</span><span id="finCSShare" style="color: #e74c3c;">-₱0.00</span></div>
            <div class="breakdown-row" style="font-weight: bold; background: #f9f9f9; padding: 5px;"><span>Combined Subtotal</span><span id="finSubtotal">₱0.00</span></div>
            <div class="breakdown-row"><span style="color: #e74c3c;">Less: 20% School IGP Share</span><span id="finIGPShare" style="color: #e74c3c;">-₱0.00</span></div>
            <div class="breakdown-row"><span style="color: #e74c3c;">Less: Production Cost</span><span id="finProdCostDeduct" style="color: #e74c3c;">-₱0.00</span></div>
            
            <div class="breakdown-row" style="border-top: 2px solid #2c3e50; padding-top: 15px; margin-top: 10px; font-size: 1.2rem; font-weight: bold; color: #27ae60;">
                <span>Net Department Income</span><span id="finNetIncome">₱0.00</span>
            </div>
            
            <button class="btn-action" style="margin-top: 20px; background-color: #f39c12; width: 100%;" onclick="window.print()">🖨️ Print Report</button>
            <button class="btn-action" style="margin-top: 10px; background-color: #95a5a6; width: 100%;" onclick="closeModals()">Close</button>
        </div>
    </div>
"""
content = content.replace('<div class="modal-overlay" id="defectiveModal">', modal_html + '\n    <div class="modal-overlay" id="defectiveModal">')

js_funcs = """
    function openFinancialModal() {
        document.getElementById('financialModal').style.display = 'flex';
    }
"""
content = content.replace('function openDefectiveModal() {', js_funcs + '\n    function openDefectiveModal() {')

# Add close functionality for financialModal
content = content.replace("document.getElementById('defectiveModal').style.display = 'none';", "document.getElementById('defectiveModal').style.display = 'none';\n        document.getElementById('financialModal').style.display = 'none';")

# Replace JS fetch block logic
# Find where statsData is populated and insert financial injection
fetch_inject = """
                    // populate financials
                    if (data.financials) {
                        document.getElementById('finModalPeriodLabel').innerText = `Period: ${data.active_period || periodQuery}`;
                        document.getElementById('finAR').innerText = '₱' + data.financials.accounts_receivable.toLocaleString(undefined, {minimumFractionDigits: 2});
                        document.getElementById('finAP').innerText = '₱' + data.financials.igp_share.toLocaleString(undefined, {minimumFractionDigits: 2});
                        document.getElementById('finProdCost').innerText = '₱' + data.financials.production_cost.toLocaleString(undefined, {minimumFractionDigits: 2});
                        
                        document.getElementById('finGross').innerText = '₱' + data.financials.gross_revenue.toLocaleString(undefined, {minimumFractionDigits: 2});
                        document.getElementById('finITShare').innerText = '-₱' + data.financials.it_share.toLocaleString(undefined, {minimumFractionDigits: 2});
                        document.getElementById('finCSShare').innerText = '-₱' + data.financials.cs_share.toLocaleString(undefined, {minimumFractionDigits: 2});
                        document.getElementById('finSubtotal').innerText = '₱' + data.financials.combined_subtotal.toLocaleString(undefined, {minimumFractionDigits: 2});
                        document.getElementById('finIGPShare').innerText = '-₱' + data.financials.igp_share.toLocaleString(undefined, {minimumFractionDigits: 2});
                        document.getElementById('finProdCostDeduct').innerText = '-₱' + data.financials.production_cost.toLocaleString(undefined, {minimumFractionDigits: 2});
                        document.getElementById('finNetIncome').innerText = '₱' + data.financials.net_income.toLocaleString(undefined, {minimumFractionDigits: 2});
                        
                        if (data.charts && data.charts.departments) {
                            statsData.revenue.cs = (data.charts.departments.CS || 0) * (data.financials.booklet_price || 10);
                            statsData.revenue.it = (data.charts.departments.IT || 0) * (data.financials.booklet_price || 10);
                        }
                    }
"""

# Replace the hardcoded revenue cs/it lines with nothing since we injected it
content = content.replace("statsData.revenue.cs = (data.charts.departments.CS || 0) * 10;", "")
content = content.replace("statsData.revenue.it = (data.charts.departments.IT || 0) * 10;", "")
content = content.replace("if (data.charts && data.charts.departments) {", fetch_inject)


with io.open('templates/dashboard.html', 'w', encoding='utf-8') as f:
    f.write(content)

