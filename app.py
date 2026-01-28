import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import requests
import time
import base64
from io import BytesIO
from pypdf import PdfWriter, PdfReader, Transformation

# --- CONFIGURATION & CONSTANTS ---
SOURCE_SHEET_ID = "1nb8gE9i3GmxquG93hLX0a5Kn_GoGH1uCESdVxtXnkv0"
LABEL_TEMPLATE_ID = "1fUuCsIumgRAmJEt-FvvaXrjDaVTT6FJtGz162ZYIwLY"
PACKING_SLIP_ID = "1fr-Mjq0rkQadr-5nvaOK5YqyCo5Teye_wS4-P1UN7po"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

COL_MAP = {
    "Num": "order_num",
    "P. O. #": "po_num",
    "Name": "customer_name",
    "Item": "vendor_sku",
    "Item Description": "description",
    "Backordered": "ordered_qty",
    "Ship To Address 1": "address_1",
    "Ship To Address 2": "address_2",
    "Other 1": "customer_sku",
    "Match Data for Address": "city_state_zip",
}

# --- HELPER FUNCTIONS ---
def truncate_text(text, max_len=55):
    """Truncates text to max_len and adds '...' if longer."""
    text = str(text)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text

# --- AUTHENTICATION ---
@st.cache_resource
def get_gspread_client():
    try:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        client = gspread.authorize(creds)
        return client, creds
    except Exception as e:
        st.error(f"Error authenticating: {e}")
        return None, None

# --- PDF ENGINE ---
def export_sheet_to_pdf(sheet_id, sheet_gid, creds, margin=0, fit=True):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {
        "format": "pdf",
        "gid": sheet_gid,
        "portrait": "true",
        "fitw": "true" if fit else "false",
        "gridlines": "false",
        "top_margin": str(margin),
        "bottom_margin": str(margin),
        "left_margin": str(margin),
        "right_margin": str(margin),
    }
    headers = {"Authorization": f"Bearer {creds.token}"}
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        headers = {"Authorization": f"Bearer {creds.token}"}

    response = requests.get(url, params=params, headers=headers)
    if response.status_code == 200:
        return response.content
    return None

def show_pdf(pdf_bytes, height=500):
    base64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
    pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="{height}" type="application/pdf"></iframe>'
    st.markdown(pdf_display, unsafe_allow_html=True)

# --- DATA LOADING ---
def load_data(client):
    try:
        sh = client.open_by_key(SOURCE_SHEET_ID)
        worksheet = sh.worksheet("Open SO")
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        relevant_cols = {k: v for k, v in COL_MAP.items() if k in df.columns}
        df = df.rename(columns=relevant_cols)
        if 'ordered_qty' in df.columns:
            df['ordered_qty'] = pd.to_numeric(df['ordered_qty'], errors='coerce').fillna(0)
        return df
    except Exception as e:
        st.error(f"Error loading source data: {e}")
        return pd.DataFrame()

# --- APP LAYOUT ---
st.set_page_config(page_title="Warehouse Control Center", layout="wide", page_icon="🏭")

st.markdown("""
<style>
    div[data-testid="stMetricValue"] { font-size: 1.2rem; }
    .stButton button { width: 100%; border-radius: 5px; font-weight: bold;}
    div.block-container { padding-top: 2rem; }
</style>
""", unsafe_allow_html=True)

client, creds = get_gspread_client()

if client:
    if "data" not in st.session_state:
        st.session_state.data = load_data(client)
        st.session_state.last_fetch = time.time()
    
    with st.sidebar:
        st.title("🏭 Operations")
        if st.button("🔄 Refresh Data"):
            st.session_state.data = load_data(client)
            st.success("Data Refreshed!")

    df = st.session_state.data

    if df.empty:
        st.warning("No open orders found. Check connection or source sheet.")
    else:
        # --- VIEW 1: DASHBOARD ---
        if "selected_order" not in st.session_state:
            st.session_state.selected_order = None

        if st.session_state.selected_order is None:
            st.title("📋 Open Orders Dashboard")
            
            unique_orders = df[['order_num', 'po_num', 'customer_name']].drop_duplicates()
            search_list = unique_orders.apply(
                lambda x: f"{x['order_num']} (PO: {x['po_num']}) - {x['customer_name']}", axis=1
            ).tolist()
            
            c1, c2 = st.columns([2, 1])
            with c1:
                selected_search = st.selectbox(
                    "🔍 Fast Search", 
                    options=[""] + search_list,
                    placeholder="Type Order #, PO, or Name...",
                    index=None
                )
            
            if selected_search:
                order_num = selected_search.split(" ")[0]
                st.session_state.selected_order = order_num
                st.rerun()

            st.markdown("### Recent Orders")
            grouped_view = df.groupby(['order_num', 'po_num', 'customer_name']).agg({
                'vendor_sku': 'count',
                'ordered_qty': 'sum'
            }).reset_index()
            grouped_view.columns = ['Order #', 'PO #', 'Customer', 'Items Count', 'Total Qty']
            
            st.dataframe(
                grouped_view,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Order #": st.column_config.TextColumn("Order #", help="Sales Order Number"),
                    "Total Qty": st.column_config.ProgressColumn("Size", format="%d", min_value=0, max_value=int(grouped_view['Total Qty'].max())),
                }
            )
            st.caption("💡 Tip: Use the search bar above to open a specific order.")

        # --- VIEW 2: ORDER CONTROL CENTER ---
        else:
            order_id = st.session_state.selected_order
            order_data = df[df['order_num'].astype(str) == str(order_id)].copy()
            
            if order_data.empty:
                st.error("Order not found.")
                if st.button("Go Back"):
                    st.session_state.selected_order = None
                    st.rerun()
            else:
                header = order_data.iloc[0]

                with st.container():
                    c1, c2, c3, c4 = st.columns([0.5, 2, 1.5, 1.5])
                    with c1:
                        if st.button("⬅️ Back"):
                            st.session_state.selected_order = None
                            st.rerun()
                    with c2:
                        st.markdown(f"### 📦 Order {header['order_num']}")
                        st.caption(f"{header['customer_name']}")
                    with c3:
                        st.metric("PO Number", str(header['po_num']))
                    with c4:
                        st.metric("Total Items", f"{len(order_data)} lines")
                
                st.divider()

                left_col, right_col = st.columns([1.8, 1])

                with left_col:
                    st.subheader("📝 Order Details & Shipping")
                    display_df = order_data[['vendor_sku', 'description', 'ordered_qty', 'customer_sku']].copy()
                    display_df['shipped_qty'] = display_df['ordered_qty']
                    
                    edited_df = st.data_editor(
                        display_df,
                        column_config={
                            "shipped_qty": st.column_config.NumberColumn("Ship Qty", min_value=0),
                            "ordered_qty": st.column_config.NumberColumn("Ordered", disabled=True),
                            "vendor_sku": st.column_config.TextColumn("SKU", disabled=True),
                            "description": st.column_config.TextColumn("Desc", disabled=True, width="medium"),
                            "customer_sku": st.column_config.TextColumn("Cust SKU", disabled=True),
                        },
                        use_container_width=True,
                        key=f"editor_{order_id}",
                        num_rows="fixed",
                        height=400
                    )

                with right_col:
                    tab1, tab2 = st.tabs(["🏷️ Item Labels", "📄 Packing Slip"])
                    
                    # === TAB 1: LABELS ===
                    with tab1:
                        st.info("Select an item to generate labels.")
                        item_to_print = st.selectbox("Select SKU:", edited_df['vendor_sku'].tolist())
                        
                        if item_to_print:
                            item_row = order_data[order_data['vendor_sku'] == item_to_print].iloc[0]
                            
                            c_lbl1, c_lbl2 = st.columns(2)
                            with c_lbl1:
                                label_qty_box = st.number_input("Qty/Box", value=int(item_row.get('ordered_qty', 1)))
                            with c_lbl2:
                                total_labels = st.number_input("# Boxes", value=1, min_value=1)
                            
                            with st.expander("⚙️ Printer Calibrate", expanded=False):
                                st.caption("Zebra ZP450 Settings")
                                rotate_label = st.checkbox("Rotate 90°", value=True)
                                scale_factor = st.slider("Scale", 0.5, 1.1, 0.95, 0.01)
                                x_offset = st.slider("Move Up/Down", -100, 100, -20, 5)
                                y_offset = st.slider("Move L/R", -100, 100, 0, 5)

                            if st.button("🖨️ Generate Labels", type="primary"):
                                with st.spinner("Generating PDF..."):
                                    try:
                                        lbl_sh = client.open_by_key(LABEL_TEMPLATE_ID)
                                        lbl_ws = lbl_sh.worksheet("ItemLabel")
                                        merger = PdfWriter()
                                        TARGET_WIDTH = 432; TARGET_HEIGHT = 288
                                        
                                        for i in range(1, int(total_labels) + 1):
                                            # TRUNCATE DESCRIPTION HERE
                                            desc_clean = truncate_text(item_row.get('description', ''), 55)

                                            updates = [
                                                {'range': 'C3', 'values': [[str(item_row.get('customer_sku', ''))]]},
                                                {'range': 'B5', 'values': [[desc_clean]]},  # Use truncated description
                                                {'range': 'B7', 'values': [[str(item_row.get('vendor_sku', ''))]]},
                                                {'range': 'C11', 'values': [[str(item_row.get('po_num', ''))]]},
                                                {'range': 'F7', 'values': [[label_qty_box]]},
                                                {'range': 'B9', 'values': [[i]]},
                                                {'range': 'D9', 'values': [[total_labels]]}
                                            ]
                                            lbl_ws.batch_update(updates)
                                            time.sleep(0.5)
                                            
                                            pdf_data = export_sheet_to_pdf(LABEL_TEMPLATE_ID, lbl_ws.id, creds, margin=0, fit=False)
                                            if pdf_data:
                                                reader = PdfReader(BytesIO(pdf_data))
                                                page = reader.pages[0]
                                                op = Transformation().scale(sx=scale_factor, sy=scale_factor).translate(tx=x_offset, ty=y_offset)
                                                page.add_transformation(op)
                                                page.mediabox.lower_left = (0, page.mediabox.top - TARGET_HEIGHT)
                                                page.mediabox.upper_right = (TARGET_WIDTH, page.mediabox.top)
                                                if rotate_label: page.rotate(90)
                                                merger.add_page(page)
                                        
                                        output_pdf = BytesIO()
                                        merger.write(output_pdf)
                                        merged_bytes = output_pdf.getvalue()
                                        
                                        st.success("Labels Ready!")
                                        st.download_button("⬇️ Download PDF", merged_bytes, file_name=f"Labels_{item_to_print}.pdf")
                                        show_pdf(merged_bytes, height=300)
                                        
                                    except Exception as e:
                                        st.error(f"Error: {e}")

                    # === TAB 2: PACKING SLIP ===
                    with tab2:
                        st.info("Generates slip for items with Ship Qty > 0")
                        ship_method = st.radio("Method", ["Small Parcel", "LTL"], horizontal=True)
                        
                        if st.button("📄 Generate Packing Slip", type="primary"):
                            with st.spinner("Building Slip..."):
                                try:
                                    ps_sh = client.open_by_key(PACKING_SLIP_ID)
                                    ps_ws = ps_sh.worksheet("Template")
                                    
                                    updates = [
                                        {'range': 'B11', 'values': [[str(header.get('customer_name', ''))]]},
                                        {'range': 'B12', 'values': [[str(header.get('address_1', ''))]]},
                                        {'range': 'B13', 'values': [[str(header.get('address_2', ''))]]},
                                        {'range': 'B14', 'values': [[str(header.get('city_state_zip', ''))]]},
                                        {'range': 'H11', 'values': [[str(header.get('order_num', ''))]]},
                                        {'range': 'H12', 'values': [[str(header.get('po_num', ''))]]},
                                        {'range': 'H13', 'values': [[ship_method]]},
                                    ]
                                    ps_ws.batch_update(updates)
                                    
                                    final_items = edited_df[edited_df['shipped_qty'] > 0]
                                    ps_ws.batch_clear(["B19:H100"])
                                    
                                    if not final_items.empty:
                                        rows_to_add = []
                                        for idx, row in final_items.iterrows():
                                            # TRUNCATE DESCRIPTION HERE
                                            desc_clean = truncate_text(row['description'], 55)
                                            
                                            rows_to_add.append([
                                                str(row['customer_sku']),
                                                str(row['vendor_sku']),
                                                int(row['ordered_qty']),
                                                int(row['shipped_qty']),
                                                desc_clean  # Use truncated description
                                            ])
                                        ps_ws.update(range_name="B19", values=rows_to_add)
                                    
                                    pdf_bytes = export_sheet_to_pdf(PACKING_SLIP_ID, ps_ws.id, creds, margin=0)
                                    if pdf_bytes:
                                        st.success("Slip Ready!")
                                        st.download_button("⬇️ Download Slip", pdf_bytes, file_name=f"PS_{order_id}.pdf")
                                        show_pdf(pdf_bytes, height=400)
                                        
                                except Exception as e:
                                    st.error(f"Error: {e}")

else:
    st.info("Please ensure 'credentials.json' is in the folder to start.")