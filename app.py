from flask import Flask, request, jsonify, send_file
import pandas as pd
import sqlite3
import os
import re
from graphene import ObjectType, String, List, Schema, Argument
from graphql_server.flask import GraphQLView
from werkzeug.utils import secure_filename
from flask_cors import CORS  # Import Flask-CORS

app = Flask(__name__)
CORS(app)  # Enable Cross-Origin Resource Sharing (CORS)

# Configuration
UPLOAD_FOLDER = 'uploads'
DATABASE = 'database.db'
ALLOWED_EXTENSIONS = {'csv'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# SQLite Connection
def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# Initialize database and create large_table if it doesn't exist
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS large_table (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        origin_file TEXT
    )
    ''')
    conn.commit()
    conn.close()

# Function to sanitize column names
def sanitize_column_name(name):
    name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    return name.strip('_')

# Function to add missing columns dynamically with sanitized names
def add_missing_columns(conn, df):
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(large_table)")
    existing_columns = {col[1] for col in cursor.fetchall()}
    
    sanitized_columns = {sanitize_column_name(col) for col in df.columns}
    new_columns = sanitized_columns - existing_columns
    
    for col in new_columns:
        print(f"Adding missing column: {col}")
        cursor.execute(f"ALTER TABLE large_table ADD COLUMN {col} TEXT")
    
    conn.commit()

# Upload CSV endpoint
@app.route('/upload', methods=['POST'])
def upload_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.csv'):
        return jsonify({'error': 'Invalid file type'}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)
    
    try:
        df = pd.read_csv(filepath)
        df.columns = [sanitize_column_name(col) for col in df.columns]
        df['origin_file'] = filename
        
        conn = get_db_connection()
        add_missing_columns(conn, df)
        df.to_sql('large_table', conn, if_exists='append', index=False)
        conn.close()
        os.remove(filepath)
        
        return jsonify({'message': 'File uploaded successfully', 'rows': len(df)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# List unique sources
@app.route('/sources', methods=['GET'])
def list_sources():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT origin_file FROM large_table")
    sources = [row['origin_file'] for row in cursor.fetchall()]
    conn.close()
    return jsonify({'sources': sources})

# Download CSV by origin_file
@app.route('/download/<origin_file>', methods=['GET'])
def download_source(origin_file):
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM large_table WHERE origin_file = ?", conn, params=(origin_file,))
    conn.close()
    
    if df.empty:
        return jsonify({'error': 'No data found for this source'}), 404
    
    temp_csv = os.path.join(UPLOAD_FOLDER, f'{origin_file}.csv')
    df.to_csv(temp_csv, index=False)
    return send_file(temp_csv, mimetype='text/csv', as_attachment=True)

# Delete data by origin_file
@app.route('/delete/<origin_file>', methods=['DELETE'])
def delete_source(origin_file):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM large_table WHERE origin_file = ?", (origin_file,))
    conn.commit()
    conn.close()
    return jsonify({'message': f'Data from {origin_file} deleted successfully'})

# Dynamically generate GraphQL schema on each request with filtering
@app.route('/graphql', methods=['POST'])
def graphql():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(large_table)")
    columns = [col[1] for col in cursor.fetchall()]
    conn.close()
    
    # Use original column names with underscores
    fields = {col: String() for col in columns}
    filter_args = {col: Argument(String) for col in columns}
    
    # Create GraphQL ObjectType with snake_case field names
    DynamicRow = type('DynamicRow', (ObjectType,), fields)
    
    class Query(ObjectType):
        data = List(DynamicRow, **filter_args)
        
        def resolve_data(self, info, **kwargs):
            conn = get_db_connection()
            query = "SELECT * FROM large_table"
            conditions = []
            params = []
            
            for col, value in kwargs.items():
                conditions.append(f"{col} = ?")
                params.append(value)
                
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
                
            # Ensure the correct number of bindings are supplied
            df = pd.read_sql_query(query, conn, params=params)
            conn.close()
            return [DynamicRow(**row) for row in df.to_dict(orient='records')]
    
    schema = Schema(query=Query, auto_camelcase=False)  # Disable auto-camelcase
    view = GraphQLView.as_view('graphql', schema=schema, graphiql=True)
    return view()

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
