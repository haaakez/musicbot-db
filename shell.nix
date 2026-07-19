{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    python311
    ffmpeg
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
    export PATH="${pkgs.ffmpeg}/bin:$PATH"
    export FFMPEG_PATH="${pkgs.ffmpeg}/bin/ffmpeg"
    
    if [ ! -d venv ]; then
      python -m venv venv
      # ensure venv sees nix-provided ffmpeg by creating a symlink inside venv/bin
      ln -sf "${pkgs.ffmpeg}/bin/ffmpeg" venv/bin/ffmpeg
      chmod +x venv/bin/ffmpeg || true
    fi

    source venv/bin/activate

    python -c 'import flask' >/dev/null 2>&1 || {
      pip install --upgrade pip setuptools wheel
      pip install "discord.py[voice]" yt-dlp Flask
    }
    
    echo "🎵 Music Bot environment ready"
  '';
}
