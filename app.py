import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from google.oauth2 import service_account
import requests
import time
import base64
from io import BytesIO
from pypdf import PdfWriter, PdfReader, Transformation

# --- CONFIGURATION ---
SOURCE_SHEET_ID = "1nb8gE9i3GmxquG93hLX0a5Kn_GoGH1uCESdVxtXnkv0"
LABEL_TEMPLATE_ID = "1fUuCsIumgRAmJEt-FvvaXrjDaVTT6FJtGz162ZYIwLY"
PACKING_SLIP_ID = "1fr-Mjq0rkQadr-5nvaOK5YqyCo5Teye_wS4-P1UN7po"

CSV_MAP = {
    "Order Number": "order_num",
    "PO Number": "po_num",
    "CustomerName": "customer_name",
    "Item": "vendor_sku",
    "ItemDescription": "description",
    "OrderedQty": "ordered_qty",
    "Ship To Address 1": "address_1",
    "Ship To Address 2": "address_2",
    "Cust SKU": "customer_sku",
    "Match Data for Address": "city_state_zip",
}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

# --- SETUP ---
st.set_page_config(page_title="Warehouse Portal", layout="wide", page_icon="📦")

# --- CUSTOM CSS ---
st.markdown("""
<style>
    /* HIDE DEFAULT ELEMENTS */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header[data-testid="stHeader"] {display: none;}

    /* LAYOUT */
    .block-container {padding-top: 1rem; padding-bottom: 5rem;}
    button[kind="primary"] {width: 100%; border-radius: 6px;}
    
    /* STICKY HEADER MAGIC */
    div[data-testid="stVerticalBlock"] > div.element-container:has(div.sticky-marker) {
        position: sticky;
        top: 0;
        z-index: 999;
        background-color: white;
        padding-top: 10px;
        padding-bottom: 10px;
        border-bottom: 2px solid #f0f2f6;
    }
</style>
""", unsafe_allow_html=True)

# --- AUTH (SERVICE ACCOUNT ONLY) ---
@st.cache_resource
def get_gspread_client():
    try:
        if "gcp_service_account" in st.secrets:
            creds_dict = dict(st.secrets["gcp_service_account"])
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        return gspread.authorize(creds), creds
    except Exception as e:
        st.error(f"Auth Error: {e}")
        return None, None

# --- HELPERS ---
def truncate_text(text, max_len=55):
    text = str(text) if text else ""
    return text[:max_len] + "..." if len(text) > max_len else text

def export_sheet_to_pdf(sheet_id, sheet_gid, creds, fit=True):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {
        "format": "pdf", "gid": sheet_gid, "portrait": "true",
        "fitw": "true" if fit else "false", "gridlines": "false"
    }
    headers = {"Authorization": f"Bearer {creds.token}"}
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        headers = {"Authorization": f"Bearer {creds.token}"}
    
    resp = requests.get(url, params=params, headers=headers)
    return resp.content if resp.status_code == 200 else None

def set_rows_visibility(spreadsheet, worksheet_id, start_row, end_row, hide=True):
    body = {
        "requests": [{"updateDimensionProperties": {
            "range": {"sheetId": worksheet_id, "dimension": "ROWS", "startIndex": start_row - 1, "endIndex": end_row},
            "properties": {"hiddenByUser": hide},
            "fields": "hiddenByUser"
        }}]
    }
    spreadsheet.batch_update(body)

def generate_single_label_pdf(item_row, label_qty, creds, client, settings):
    lbl_sh = client.open_by_key(LABEL_TEMPLATE_ID)
    lbl_ws = lbl_sh.worksheet("ItemLabel")
    
    desc_clean = truncate_text(item_row.get('description', ''), 55)
    
    updates = [
        {'range': 'C3', 'values': [[str(item_row.get('customer_sku', ''))]]},
        {'range': 'B5', 'values': [[desc_clean]]},
        {'range': 'B7', 'values': [[str(item_row.get('vendor_sku', ''))]]},
        {'range': 'C11', 'values': [[str(item_row.get('po_num', ''))]]},
        {'range': 'F7', 'values': [[label_qty]]}
    ]
    lbl_ws.batch_update(updates)
    time.sleep(0.8)
    
    pdf_raw = export_sheet_to_pdf(LABEL_TEMPLATE_ID, lbl_ws.id, creds, fit=False)
    
    if pdf_raw:
        reader = PdfReader(BytesIO(pdf_raw))
        page = reader.pages[0]
        op = Transformation().scale(sx=settings['scale'], sy=settings['scale']).translate(tx=settings['x'], ty=settings['y'])
        page.add_transformation(op)
        page.mediabox.lower_left = (0, page.mediabox.top - 288) 
        page.mediabox.upper_right = (432, page.mediabox.top)
        if settings['rotate']: page.rotate(90)
        return page
    return None

# --- SCREEN 1: UPLOAD DATA ---
def upload_interface(client):
    st.title("📤 Upload Data")
    st.info("Upload CSV files to overwrite the current open orders.")
    
    uploaded_files = st.file_uploader("Choose CSVs", accept_multiple_files=True, type="csv")
    
    if st.button("🚀 Process & Update Database", type="primary"):
        if uploaded_files:
            with st.spinner("Updating Database..."):
                try:
                    all_dfs = []
                    for uploaded_file in uploaded_files:
                        df = pd.read_csv(uploaded_file)
                        df.columns = df.columns.str.strip()
                        rename_map = {k: v for k, v in CSV_MAP.items() if k in df.columns}
                        df = df.rename(columns=rename_map)
                        valid_cols = [v for k, v in CSV_MAP.items() if v in df.columns]
                        df = df[valid_cols]
                        all_dfs.append(df)
                    
                    if all_dfs:
                        final_df = pd.concat(all_dfs, ignore_index=True).fillna("")
                        sh = client.open_by_key(SOURCE_SHEET_ID)
                        ws = sh.worksheet("Open SO")
                        ws.clear()
                        ws.update(range_name="A1", values=[final_df.columns.values.tolist()])
                        ws.update(range_name="A2", values=final_df.values.tolist())
                        st.success(f"Uploaded {len(final_df)} lines!")
                except Exception as e:
                    st.error(f"Error: {e}")

# --- SCREEN 2: WAREHOUSE OPS ---
def warehouse_interface(client, creds):
    # Load Data
    @st.cache_data(ttl=60)
    def load_data():
        try:
            sh = client.open_by_key(SOURCE_SHEET_ID)
            return pd.DataFrame(sh.worksheet("Open SO").get_all_records())
        except: return pd.DataFrame()

    df = load_data()
    
    # --- DASHBOARD VIEW ---
    if "selected_order" not in st.session_state:
        st.session_state.selected_order = None

    if st.session_state.selected_order is None:
        c1, c2 = st.columns([0.8, 0.2])
        with c1: st.title("📋 Open Orders")
        with c2: 
            if st.button("🔄 Refresh"):
                st.cache_data.clear()
                st.rerun()
        
        if df.empty:
            st.info("No active orders found.")
            return

        filter_txt = st.text_input("Find Order...", placeholder="Type Order #, PO, or Customer Name")
        
        view_df = df[['order_num', 'po_num', 'customer_name', 'vendor_sku', 'ordered_qty']].copy()
        grouped = view_df.groupby(['order_num', 'po_num', 'customer_name']).agg({
            'vendor_sku': 'count',
            'ordered_qty': 'sum'
        }).reset_index().rename(columns={'vendor_sku': 'Items', 'ordered_qty': 'Total Qty'})
        
        if filter_txt:
            grouped = grouped[
                grouped['order_num'].astype(str).str.contains(filter_txt, case=False) | 
                grouped['customer_name'].str.contains(filter_txt, case=False) |
                grouped['po_num'].astype(str).str.contains(filter_txt, case=False)
            ]

        st.caption("Click any row below to open the order.")
        
        event = st.dataframe(
            grouped,
            use_container_width=True,
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            height=600
        )
        
        if len(event.selection.rows) > 0:
            idx = event.selection.rows[0]
            order_id = grouped.iloc[idx]['order_num']
            st.session_state.selected_order = str(order_id)
            st.rerun()

    # --- ORDER DETAIL VIEW ---
    else:
        order_id = st.session_state.selected_order
        order_data = df[df['order_num'].astype(str) == str(order_id)].copy()
        
        if order_data.empty:
            st.error("Order not found.")
            st.session_state.selected_order = None
            st.rerun()
            
        header = order_data.iloc[0]

        # === STICKY HEADER ===
        with st.container():
            st.markdown('<div class="sticky-marker"></div>', unsafe_allow_html=True)
            
            c1, c2 = st.columns([0.15, 0.85])
            with c1:
                if st.button("⬅️ BACK", key="back_btn", type="secondary"):
                    st.session_state.selected_order = None
                    st.rerun()
            with c2:
                st.markdown(f"### {header['customer_name']}")
                st.markdown(f"**SO:** {header['order_num']} &nbsp; | &nbsp; **PO:** {header['po_num']}", unsafe_allow_html=True)
        # === END STICKY HEADER ===

        st.write("") 
        col_table, col_actions = st.columns([2, 1])

        # 1. TABLE
        with col_table:
            st.subheader("Items")
            display_df = order_data[['vendor_sku', 'description', 'ordered_qty', 'customer_sku']].copy()
            display_df['shipped_qty'] = display_df['ordered_qty']
            
            edited_df = st.data_editor(
                display_df,
                column_config={
                    "vendor_sku": st.column_config.TextColumn("SKU", disabled=True),
                    "description": st.column_config.TextColumn("Description", disabled=True),
                    "ordered_qty": st.column_config.NumberColumn("Ord", disabled=True, width="small"),
                    "shipped_qty": st.column_config.NumberColumn("Ship", min_value=0, width="small"),
                    "customer_sku": None 
                },
                use_container_width=True,
                hide_index=True,
                key=f"editor_{order_id}",
                height=500
            )

        # 2. ACTIONS
        with col_actions:
            st.subheader("Print")
            
            tab_single, tab_batch, tab_slip = st.tabs(["Single Label", "All Labels", "Packing Slip"])
            
            with st.expander("⚙️ Printer Settings"):
                p_rotate = st.checkbox("Rotate 90°", value=True)
                p_scale = st.slider("Scale", 0.5, 1.2, 0.95, 0.05)
                p_x = st.slider("Vertical Offset", -100, 100, -20, 5)
                p_y = st.slider("Horizontal Offset", -100, 100, 0, 5)
                settings = {'rotate': p_rotate, 'scale': p_scale, 'x': p_x, 'y': p_y}

            with tab_single:
                item_sku = st.selectbox("Select Item:", edited_df['vendor_sku'].unique())
                item_row = order_data[order_data['vendor_sku'] == item_sku].iloc[0]
                qty_box = st.number_input("Qty on Label", value=int(item_row.get('ordered_qty', 1)))
                
                if st.button(f"Generate '{item_sku}' Label", key="btn_single"):
                    with st.spinner("Processing..."):
                        merger = PdfWriter()
                        page = generate_single_label_pdf(item_row, qty_box, creds, client, settings)
                        if page: merger.add_page(page)
                        out = BytesIO()
                        merger.write(out)
                        st.download_button("⬇️ Download PDF", out.getvalue(), f"Label_{item_sku}.pdf", mime="application/pdf", type="primary")

            with tab_batch:
                st.info("Labels for ALL items.")
                if st.button("Generate ALL Labels", key="btn_batch"):
                    with st.spinner("Processing Batch..."):
                        merger = PdfWriter()
                        for idx, row in edited_df.iterrows():
                            sku = row['vendor_sku']
                            full_row = order_data[order_data['vendor_sku'] == sku].iloc[0]
                            qty = row['ordered_qty']
                            page = generate_single_label_pdf(full_row, qty, creds, client, settings)
                            if page: merger.add_page(page)
                        out = BytesIO()
                        merger.write(out)
                        st.success("Ready!")
                        st.download_button("⬇️ Download Batch PDF", out.getvalue(), f"Batch_{order_id}.pdf", mime="application/pdf", type="primary")

            with tab_slip:
                method = st.radio("Method", ["Small Parcel", "LTL"])
                if st.button("Generate Slip", key="btn_slip"):
                    with st.spinner("Creating Slip..."):
                        ps_sh = client.open_by_key(PACKING_SLIP_ID)
                        ps_ws = ps_sh.worksheet("Template")
                        
                        set_rows_visibility(ps_sh, ps_ws.id, 19, 100, hide=False)
                        
                        updates = [
                            {'range': 'B11', 'values': [[str(header.get('customer_name', ''))]]},
                            {'range': 'B12', 'values': [[str(header.get('address_1', ''))]]},
                            {'range': 'B13', 'values': [[str(header.get('address_2', ''))]]},
                            {'range': 'B14', 'values': [[str(header.get('city_state_zip', ''))]]},
                            {'range': 'H11', 'values': [[str(header.get('order_num', ''))]]},
                            {'range': 'H12', 'values': [[str(header.get('po_num', ''))]]},
                            {'range': 'H13', 'values': [[method]]},
                        ]
                        ps_ws.batch_update(updates)
                        
                        final_items = edited_df[edited_df['shipped_qty'] > 0]
                        ps_ws.batch_clear(["B19:H100"])
                        
                        num_lines = 0
                        if not final_items.empty:
                            rows = []
                            for _, row in final_items.iterrows():
                                rows.append([
                                    str(row['customer_sku']),
                                    str(row['vendor_sku']),
                                    int(row['ordered_qty']),
                                    int(row['shipped_qty']),
                                    truncate_text(row['description'], 55)
                                ])
                            ps_ws.update(range_name="B19", values=rows)
                            num_lines = len(rows)

                        set_rows_visibility(ps_sh, ps_ws.id, 19 + num_lines, 150, hide=True)
                        
                        pdf_bytes = export_sheet_to_pdf(PACKING_SLIP_ID, ps_ws.id, creds)
                        if pdf_bytes:
                            st.download_button("⬇️ Download Slip", pdf_bytes, f"PS_{order_id}.pdf", mime="application/pdf", type="primary")

# --- MAIN EXECUTION ---
client, creds = get_gspread_client()

if not client:
    st.error("Authentication failed. Check your Streamlit Secrets.")
else:
    # SIDEBAR NAV
    nav_option = st.sidebar.radio("Menu", ["📦 Warehouse Ops", "📤 Upload Data"])
    
    if nav_option == "📦 Warehouse Ops":
        warehouse_interface(client, creds)
    else:
        upload_interface(client)
