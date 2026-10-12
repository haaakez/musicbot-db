{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    python311
    jdk17
    nodejs_22
    deno
    libsodium
    pkg-config
    gcc
    gnumake
    libffi
  ];
  
  shellHook = ''
    export LD_LIBRARY_PATH="${pkgs.libsodium}/lib:${pkgs.libffi}/lib:$LD_LIBRARY_PATH"
    export LDFLAGS="-L${pkgs.libsodium}/lib -L${pkgs.libffi}/lib"
    export CPPFLAGS="-I${pkgs.libsodium}/include -I${pkgs.libffi}/include"
    export PKG_CONFIG_PATH="${pkgs.libsodium}/lib/pkgconfig:$PKG_CONFIG_PATH"
    
    # Explicitly force Java into the environment PATH
    export PATH="${pkgs.jdk17}/bin:$PATH"
    
    if [ ! -d venv ]; then
      python -m venv venv
    fi

    source venv/bin/activate

    python -c 'import wavelink, yt_dlp, yt_dlp_ejs; import importlib.metadata; importlib.metadata.version("bgutil-ytdlp-pot-provider")' >/dev/null 2>&1 || {
      pip install --upgrade pip setuptools wheel
      pip install -r requirements.txt
    }
    
    echo "🎵 Music Bot & Lavalink environment ready"
    echo "▶️ Run 'java -jar Lavalink.jar' in one terminal"
    echo "▶️ Run 'python main.py' in another!"
  '';
}