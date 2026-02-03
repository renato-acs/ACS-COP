import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from google.oauth2 import service_account
import requests
import time
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
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header[data-testid="stHeader"] {display: none;}
    .block-container {padding-top: 1rem; padding-bottom: 5rem;}
    html, body, [class*="css"] {font-size: 18px !important;}
    button[kind="primary"] {width: 100%; border-radius: 6px; font-weight: bold; font-size: 18px !important; padding: 0.5rem 1rem;}
    button[kind="secondary"] {width: 100%; border-radius: 6px;}
    div[data-testid="stVerticalBlock"] > div.element-container:has(div.sticky-marker) {
        position: sticky; top: 0; z-index: 999; background-color: white;
        padding-top: 15px; padding-bottom: 15px; border-bottom: 3px solid #f0f2f6;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    }
    div[data-testid="stDataEditor"] table {font-size: 18px !important;}
    div[data-testid="stDataEditor"] tr:nth-of-type(even) {background-color: #f9f9f9;}
    div[data-testid="stDataEditor"] tr:hover {background-color: #e6f7ff;}
    div[role="radiogroup"] {
        background-color: #f0f2f6; padding: 10px; border-radius: 8px;
        display: flex; justify-content: center; margin-bottom: 20px;
    }
</style>
""", unsafe_allow_html=True)

# --- AUTH ---
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

def export_sheet_to_pdf(sheet_id, sheet_gid, creds, fit=True, margin=0):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {
        "format": "pdf", "gid": sheet_gid, "portrait": "true",
        "fitw": "true" if fit else "false", "gridlines": "false",
        "top_margin": str(margin), "bottom_margin": str(margin),
        "left_margin": str(margin), "right_margin": str(margin)
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

# --- GENERATE ITEM LABEL (THERMAL) ---
def generate_single_label_pdf(item_row, label_qty, creds, client, settings):
    lbl_sh = client.open_by_key(LABEL_TEMPLATE_ID)
    lbl_ws = lbl_sh.worksheet("ItemLabel")
    
    desc_clean = truncate_text(item_row.get('description', ''), 55)
    
    updates = [
        {'range': 'C3', 'values': [[str(item_row.get('customer_sku', ''))]]},
        {'range': 'B5', 'values': [[desc_clean]]},
        {'range': 'B7', 'values': [[str(item_row.get('vendor_sku', ''))]]},
        {'range': 'C11', 'values': [[str(item_row.get('po_num', ''))]]},
        {'range': 'F7', 'values': [[label_qty]]},
        {'range': 'E6', 'values': [[str(item_row.get('order_num', ''))]]},
        {'range': 'B9', 'values': [[str(item_row.get('customer_name', ''))]]}
    ]
    lbl_ws.batch_update(updates)
    
    # Sleep to ensure Google processes the update before export
    time.sleep(0.8)
    
    pdf_raw = export_sheet_to_pdf(LABEL_TEMPLATE_ID, lbl_ws.id, creds, fit=False, margin=0)
    
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

# --- GENERATE PALLET LABEL (UPDATED FOR THERMAL) ---
def generate_pallet_label_pdf(header_data, creds, client):
    lbl_sh = client.open_by_key(LABEL_TEMPLATE_ID)
    try:
        lbl_ws = lbl_sh.worksheet("PalletLabel")
    except:
        st.error("Sheet 'PalletLabel' not found in the template!")
        return None

    updates = [
        {'range': 'C3', 'values': [[str(header_data.get('po_num', ''))]]},
        {'range': 'C5', 'values': [[str(header_data.get('address_1', ''))]]},
        {'range': 'C6', 'values': [[str(header_data.get('address_2', ''))]]},
        {'range': 'C7', 'values': [[str(header_data.get('city_state_zip', ''))]]},
        {'range': 'C9', 'values': [[str(header_data.get('order_num', ''))]]}
    ]
    lbl_ws.batch_update(updates)
    time.sleep(1.0) 
    
    # 1. Export Raw (Fit=False for thermal precision)
    pdf_raw = export_sheet_to_pdf(LABEL_TEMPLATE_ID, lbl_ws.id, creds, fit=False, margin=0)
    
    # 2. Apply Transformations (Same as Item Label)
    if pdf_raw:
        reader = PdfReader(BytesIO(pdf_raw))
        page = reader.pages[0]
        
        # Hardcoded settings to match Item Label defaults
        settings = {'rotate': True, 'scale': 0.95, 'x': -5, 'y': 25}
        
        op = Transformation().scale(sx=settings['scale'], sy=settings['scale']).translate(tx=settings['x'], ty=settings['y'])
        page.add_transformation(op)
        
        # Crop to 4x6 (Standard Thermal)
        page.mediabox.lower_left = (0, page.mediabox.top - 288) 
        page.mediabox.upper_right = (432, page.mediabox.top)
        
        # Rotate 90 degrees
        if settings['rotate']: page.rotate(90)
        
        # 3. Write single page to bytes for download
        output = BytesIO()
        writer = PdfWriter()
        writer.add_page(page)
        writer.write(output)
        return output.getvalue()

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
                st.markdown(f"## {header['customer_name']}")
                st.markdown(f"<h5>SO: {header['order_num']} &nbsp;|&nbsp; PO: {header['po_num']}</h5>", unsafe_allow_html=True)
        # === END STICKY HEADER ===

        st.write("") 
        
        col_table, col_actions = st.columns([0.65, 0.35])

        # 1. TABLE (Left)
        with col_table:
            st.subheader("Items & Shipping")
            display_df = order_data[['vendor_sku', 'description', 'ordered_qty', 'customer_sku']].copy()
            display_df['shipped_qty'] = display_df['ordered_qty']
            display_df = display_df[['shipped_qty', 'vendor_sku', 'description', 'ordered_qty']]
            
            edited_df = st.data_editor(
                display_df,
                column_config={
                    "shipped_qty": st.column_config.NumberColumn("✏️ SHIP QTY", min_value=0, width="small", help="Edit this to change quantity on Packing Slip"),
                    "vendor_sku": st.column_config.TextColumn("SKU", disabled=True, width="medium"),
                    "description": st.column_config.TextColumn("Description", disabled=True),
                    "ordered_qty": st.column_config.NumberColumn("Ord", disabled=True, width="small"),
                },
                use_container_width=True,
                hide_index=True,
                key=f"editor_{order_id}",
                height=600
            )

        # 2. ACTIONS (Right)
        with col_actions:
            tab_labels, tab_slip = st.tabs(["🏷️ LABELS", "📄 PACKING SLIP"])
            
            # --- TAB 1: LABELS (With Batch Print Fix) ---
            with tab_labels:
                if st.button("🖨️ PRINT ALL LABELS", type="primary", key="print_all_top"):
                    msg = "Processing request. This may take a few minutes (approx 3s per label)..."
                    with st.spinner(msg):
                        merger = PdfWriter()
                        settings = {'rotate': True, 'scale': 0.95, 'x': -5, 'y': 25} 
                        
                        items_to_process = edited_df[edited_df['shipped_qty'] > 0]
                        total_items = len(items_to_process)
                        progress_bar = st.progress(0)
                        
                        for i, (idx, row) in enumerate(items_to_process.iterrows()):
                            sku = row['vendor_sku']
                            
                            # --- FIX 1: FORCE STRING COMPARISON HERE ---
                            # We convert both the column and the variable to string (.astype(str) and str())
                            full_row = order_data[order_data['vendor_sku'].astype(str) == str(sku)].iloc[0]
                            
                            qty = row['shipped_qty'] 
                            
                            if qty > 0:
                                page = generate_single_label_pdf(full_row, qty, creds, client, settings)
                                if page: merger.add_page(page)
                                time.sleep(3)
                            
                            progress_bar.progress((i + 1) / total_items)

                        progress_bar.empty()
                        out = BytesIO()
                        merger.write(out)
                        
                        st.success("Batch ready!")
                        st.download_button("⬇️ Download Batch PDF", out.getvalue(), f"Batch_{order_id}.pdf", mime="application/pdf", type="primary")
                
                st.divider()
                st.markdown("##### Individual Items")

                # Individual Label Loop
                current_settings = {'rotate': True, 'scale': 0.95, 'x': -5, 'y': 25}
                for idx, row in edited_df.iterrows():
                    sku = row['vendor_sku']
                    with st.container():
                        c_sku, c_qty, c_btn = st.columns([0.4, 0.3, 0.3])
                        with c_sku: st.markdown(f"**{sku}**")
                        with c_qty: qty_val = st.number_input("Qty", value=int(row['shipped_qty']), min_value=1, key=f"qty_{sku}_{order_id}", label_visibility="collapsed")
                        with c_btn:
                            if st.button("Print", key=f"btn_{sku}_{order_id}"):
                                with st.spinner("..."):
                                    
                                    # --- FIX 2: FORCE STRING COMPARISON HERE TOO ---
                                    item_row = order_data[order_data['vendor_sku'].astype(str) == str(sku)].iloc[0]
                                    
                                    merger = PdfWriter()
                                    page = generate_single_label_pdf(item_row, qty_val, creds, client, current_settings)
                                    if page: merger.add_page(page)
                                    out = BytesIO()
                                    merger.write(out)
                                    st.session_state[f"pdf_{sku}"] = out.getvalue()

                        if f"pdf_{sku}" in st.session_state:
                            st.download_button("⬇️", st.session_state[f"pdf_{sku}"], f"Label_{sku}.pdf", mime="application/pdf", key=f"dl_{sku}")
                        st.divider()
                        
                with st.expander("⚙️ Settings"):
                    st.checkbox("Rotate 90°", value=True, disabled=True)
                    st.caption("Default settings active.")

            # --- TAB 2: PACKING SLIP (With New Pallet Button) ---
            with tab_slip:
                st.info("Generates slip using 'Ship Qty' from the table.")
                method = st.radio("Ship Method", ["Small Parcel", "LTL"])
                st.write("")

                # Side-by-Side Buttons
                col_slip_btn, col_pallet_btn = st.columns([0.5, 0.5])

                # 1. Standard Packing Slip Button
                with col_slip_btn:
                    if st.button("📄 Packing Slip", type="primary", key="btn_slip_main"):
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
                                        str(order_data[order_data['vendor_sku']==row['vendor_sku']].iloc[0]['customer_sku']),
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
                                st.session_state['ps_pdf'] = pdf_bytes

                    if 'ps_pdf' in st.session_state:
                         st.download_button("⬇️ Download Slip", st.session_state['ps_pdf'], f"PS_{order_id}.pdf", mime="application/pdf")

                # 2. Conditional Pallet Label Button
                with col_pallet_btn:
                    if method == "LTL":
                        if st.button("📦 Pallet Label", key="btn_pallet"):
                            with st.spinner("Generating Label..."):
                                pdf_bytes = generate_pallet_label_pdf(header, creds, client)
                                if pdf_bytes:
                                    st.session_state['plt_pdf'] = pdf_bytes
                        
                        if 'plt_pdf' in st.session_state:
                            st.download_button("⬇️ Download Label", st.session_state['plt_pdf'], f"Pallet_{order_id}.pdf", mime="application/pdf")

# --- MAIN EXECUTION ---
client, creds = get_gspread_client()

if not client:
    st.error("Authentication failed. Check your Streamlit Secrets.")
else:
    nav_option = st.radio("Menu", ["📦 Warehouse Ops", "📤 Upload Data"], horizontal=True, label_visibility="collapsed")
    st.write("") 

    if nav_option == "📦 Warehouse Ops":
        warehouse_interface(client, creds)
    else:
        upload_interface(client)




