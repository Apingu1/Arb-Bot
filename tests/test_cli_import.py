def test_cli_module_imports():
    import arb_bot.main as main

    assert callable(main.cli)
